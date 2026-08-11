"""主题看板(定制数据集)— 配置读写 + 数据组装

概念:主题(Board)= 一组指标(indicator 键)的预定义查询切片。用户每天打开主题
看该组指标的最新数据(快照),或拉全时间序列(宽表);下游系统按主题键一键拉取。

职责划分:
- `BoardStore`:   config/themes.yaml 的读写与校验(零数据依赖)
- `BoardService`: 把主题组装成 DataFrame(纯查询,喂 GUI 与 API 共用)

数据路径完全复用指标归一层:
    repo.get_indicator(key) → {date, value}  →  compute_transform → level/yoy/mom

日期窗口语义:board 级 date_start/date_end + item 级可选覆盖(null=继承)。
「始终最新」= 起止均 null(查全量,快照取最新值)。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

from src.core.transform import TRANSFORMS, compute_transform
from src.utils.config import ConfigManager
from src.utils.date_parse import normalize_cn_date_str
from src.utils.logger import logger

# 主题键格式:小写字母数字下划线
KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_SAVE_LOCK = threading.Lock()

# themes.yaml 文件头(写回时保留)
THEMES_HEADER = (
    "# Bern_Financial_Data 主题看板(定制数据集)配置\n"
    "# GUI「数据 → 主题看板」编辑; 字段说明见各键注释\n"
)


def default_themes_path() -> Path:
    """config/themes.yaml 的路径(缺文件由 BoardStore 懒创建)"""
    return Path(ConfigManager().root_dir) / "config" / "themes.yaml"


def collect_indicator_keys(registry) -> set[str]:
    """从数据源目录收集所有声明了 indicator 键的集合(校验候选用)"""
    keys: set[str] = set()
    for s in registry.get_all_sources():
        ind = s.get("indicator")
        if ind:
            keys.add(str(ind))
    return keys


# ---------------------------------------------------------------------------
# 条目类型与代码类数据源判定 —— 树选择器 / 校验 / 组装三方共用同一口径
# ---------------------------------------------------------------------------

# 代码类条目的值列候选(OHLC + 成交额)
CODE_VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "amount")


def _item_type(item: dict) -> str:
    """条目类型: 'indicator'(默认, 兼容老配置) | 'code'"""
    t = str(item.get("type") or "").strip().lower()
    return "code" if t == "code" else "indicator"


def resolve_code_column(source: dict, repo) -> str | None:
    """解析代码类数据源的代码列名

    优先目录 `code_column` 声明(基金/股票有, 指数无); 否则探测表内 code/symbol
    列。读侧不剥前缀——表内存什么格式就查什么(_strip_exchange 只在同步写库用)。
    """
    table = source.get("table_name")
    cc = source.get("code_column")
    if cc:
        if repo is None or table is None:
            return cc
        if not repo.table_exists(table) or cc in repo.get_all_existing_columns(table):
            return cc
    if repo is not None and table:
        try:
            cols = repo.get_all_existing_columns(table)
        except Exception:
            cols = []
        for c in ("code", "symbol"):
            if c in cols:
                return c
    return None


def classify_source(source: dict, repo) -> str | None:
    """叶节点数据源分类: 'indicator' | 'code' | None(不可选/deprecated)"""
    if source.get("deprecated"):
        return None
    if source.get("indicator"):
        return "indicator"
    if source.get("table_name") and source.get("api_function") \
            and resolve_code_column(source, repo):
        return "code"
    return None


def collect_code_sources(registry, repo=None,
                         include_deprecated=True) -> dict[str, dict]:
    """table_name → {source_key, name, table_name, code_column, deprecated}

    供 validate_board 校验(含 deprecated 表也登记, 以便 reject); 不含 indicator
    的源中, 能解析出代码列的才算代码类数据源。
    """
    out: dict[str, dict] = {}
    for s in registry.get_all_sources(include_deprecated=True):
        if s.get("indicator"):
            continue
        if not s.get("table_name") or not s.get("api_function"):
            continue
        code_col = resolve_code_column(s, repo)
        if not code_col:
            continue
        if not include_deprecated and s.get("deprecated"):
            continue
        out[str(s["table_name"])] = {
            "source_key": s.get("source_key", ""),
            "name": s.get("name", ""),
            "table_name": str(s["table_name"]),
            "code_column": code_col,
            "deprecated": bool(s.get("deprecated")),
        }
    return out


def board_sync_targets(board: dict, repo, registry) -> list[tuple[str, dict | None]]:
    """主题可同步目标: [(source_key, params_override)]

    「同步本主题」= 逐个同步主题所引用的数据源:
    - indicator 条目 → meta_indicator 获信源表所在目录源 (params=None, 同步整表)
    - code 条目 → 其 table 所在目录源, params_override 注入该代码
      (ts_code 用 normalize_ts_code 补交易所后缀; symbol/code 原值),
      与 main_window 代码同步同一口径
    - 去重: 同 (source_key, params) 只保留一次(多 code 同表各自保留)

    返回空 → 主题无可同步条目(主题为空 / 所有 indicator 无获信源 / 表不在目录)。
    """
    targets: dict[tuple[str, str | None], tuple[str, dict | None]] = {}
    sources = registry.get_all_sources()
    for it in board.get("items", []):
        if it.get("type") == "code":
            table = str(it.get("table") or "")
            code = str(it.get("code") or "").strip()
            if not table or not code:
                continue
            src = next((s for s in sources
                        if s.get("table_name") == table and not s.get("deprecated")), None)
            if src is None:
                continue
            key = str(src.get("source_key") or "")
            if not key:
                continue
            pt = src.get("params_template") or {}
            code_param = next((k for k in ("ts_code", "symbol", "code") if k in pt), None)
            if not code_param:
                params: dict | None = None
            elif code_param == "ts_code":
                from src.core.sync_engine import normalize_ts_code
                params = {"ts_code": normalize_ts_code(code)}
            else:
                params = {code_param: code}
            targets[(key, str(params or ""))] = (key, params)
        else:
            ind = str(it.get("indicator") or "")
            if not ind:
                continue
            mapping = repo.get_indicator_map(ind) if hasattr(repo, "get_indicator_map") else None
            if not mapping:
                continue
            table = mapping.get("preferred_table")
            src = next((s for s in sources if s.get("table_name") == table), None)
            if src is None:
                continue
            key = str(src.get("source_key") or "")
            if key:
                targets[(key, None)] = (key, None)
    return list(targets.values())


def code_value_columns(source: dict, repo) -> list[str]:
    """代码条目的值列候选: OHLC 列中表内真实存在的; 表未同步用 column_map/默认"""
    table = source.get("table_name")
    existing: list[str] = []
    if table and repo is not None and repo.table_exists(table):
        try:
            existing = repo.get_all_existing_columns(table)
        except Exception:
            existing = []
    if existing:
        present = [c for c in CODE_VALUE_COLUMNS if c in existing]
        if present:
            return present
    cm = source.get("column_map") or {}
    mapped = [v for v in cm.values() if v in CODE_VALUE_COLUMNS]
    return mapped or list(CODE_VALUE_COLUMNS)


# ---------------------------------------------------------------------------
# BoardStore — 配置读写
# ---------------------------------------------------------------------------


class BoardStore:
    """主题配置的读写与校验(原子写回 + 备份)"""

    def __init__(self, path: Path | None = None):
        self.path = path or default_themes_path()

    # ---- 读取 ----

    def load(self) -> dict:
        """读取全部主题,返回 {"boards": [...]};缺文件返回空结构"""
        if not self.path.exists():
            return {"boards": []}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict) or not isinstance(data.get("boards"), list):
                logger.warning("themes.yaml 格式异常,按空主题处理: %s", self.path)
                return {"boards": []}
            return data
        except Exception as exc:
            logger.error("读取主题配置失败: %s", exc)
            return {"boards": []}

    def list_boards(self) -> list[dict]:
        """主题列表(含派生字段 item_count/date_start/date_end 归一化)"""
        boards = self.load().get("boards", [])
        out = []
        for b in boards:
            items = b.get("items", []) or []
            out.append({
                "key": b.get("key", ""),
                "name": b.get("name", ""),
                "description": b.get("description", ""),
                "item_count": len(items),
                "date_start": b.get("date_start"),
                "date_end": b.get("date_end"),
            })
        return out

    def get_board(self, key: str) -> dict | None:
        """按 key 取单个主题(原样 dict);无 → None"""
        for b in self.load().get("boards", []):
            if b.get("key") == key:
                return b
        return None

    # ---- 写入 ----

    def save(self, boards: list[dict]) -> bool:
        """写回 themes.yaml(原子替换 + 备份 + 文件头),返回是否成功"""
        with _SAVE_LOCK:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # 1. 备份
                if self.path.exists():
                    shutil.copy2(self.path, str(self.path) + ".bak")
                # 2. 文件头 + 临时文件
                text = THEMES_HEADER + yaml.safe_dump(
                    {"boards": boards}, allow_unicode=True,
                    sort_keys=False, indent=2)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                # 3. 校验临时文件可解析回
                with open(tmp, "r", encoding="utf-8") as f:
                    reloaded = yaml.safe_load(f)
                if not isinstance(reloaded, dict) or "boards" not in reloaded:
                    raise ValueError("临时文件解析失败,取消保存")
                # 4. 原子替换
                os.replace(tmp, self.path)
                logger.info("已保存主题配置到 %s(备份 .bak)", self.path)
                return True
            except Exception as exc:
                logger.error("保存主题配置失败: %s", exc)
                return False

    def add_board(self, board: dict) -> bool:
        """新增主题(校验通过才写回)"""
        if not board.get("key"):
            return False
        boards = self.load().get("boards", [])
        if any(b.get("key") == board["key"] for b in boards):
            logger.error("主题 key 已存在: %s", board["key"])
            return False
        boards.append(board)
        return self.save(boards)

    def update_board(self, key: str, board: dict) -> bool:
        """按 key 更新主题;不存在 → False"""
        boards = self.load().get("boards", [])
        for i, b in enumerate(boards):
            if b.get("key") == key:
                boards[i] = board
                return self.save(boards)
        return False

    def delete_board(self, key: str) -> bool:
        """删除主题"""
        boards = self.load().get("boards", [])
        remaining = [b for b in boards if b.get("key") != key]
        if len(remaining) == len(boards):
            return False
        return self.save(remaining)

    # ---- 校验 ----

    @staticmethod
    def validate_board(board: dict, all_boards: list[dict] | None = None,
                       known_indicators: set[str] | None = None,
                       known_code_sources: dict[str, dict] | None = None) -> list[str]:
        """校验单个主题,返回错误列表(空 = 通过)

        - key 必填且格式 ^[a-z0-9_]+$、全局唯一(所有主题中)
        - name 非空
        - items 非空列表,每项按类型(默认 indicator)校验:
          - indicator: indicator 必填且在 known_indicators 内(传了才查)、
            transform ∈ {level, yoy, mom, pct}
          - code: table/code_column/code/value_column 非空; known_code_sources
            传了时 table ∈ dict、非 deprecated、code_column 与目录一致
        - date_start/date_end(若有)必须是可解析日期(公共段, 两态共用)
        """
        errors: list[str] = []
        key = str(board.get("key", "") or "")
        name = str(board.get("name", "") or "")

        if not key:
            errors.append("主题缺少 key")
        elif not KEY_PATTERN.match(key):
            errors.append(f"key 只能用小写字母/数字/下划线,当前: {key}")

        if not name:
            errors.append(f"主题 {key or '(无key)'} 缺少名称")

        if all_boards:
            for other in all_boards:
                if other.get("key") == key and other is not board:
                    errors.append(f"主题 key 重复: {key}")
                    break

        items = board.get("items")
        if not isinstance(items, list) or not items:
            errors.append(f"主题 {name or key} 至少需要一个指标")
        else:
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    errors.append(f"第 {i + 1} 个条目格式错误")
                    continue
                if _item_type(it) == "code":
                    BoardStore._validate_code_item(it, i, known_code_sources, errors)
                else:
                    BoardStore._validate_indicator_item(it, i, known_indicators, errors)
                # 日期覆盖字段(公共段, 两态共用)
                for field in ("date_start", "date_end"):
                    v = it.get(field)
                    if v is None or v == "":
                        continue
                    if not _parse_date(v):
                        label = it.get("indicator") or it.get("code") or i + 1
                        errors.append(f"条目 {label} 的 {field} 不是有效日期: {v}")

        for field in ("date_start", "date_end"):
            v = board.get(field)
            if v is None or v == "":
                continue
            if not _parse_date(v):
                errors.append(f"主题 {name or key} 的 {field} 不是有效日期: {v}")

        return errors

    @staticmethod
    def _validate_indicator_item(it: dict, i: int,
                                 known_indicators: set[str] | None,
                                 errors: list[str]) -> None:
        """校验 indicator 条目(向后兼容: 老条目无 type 也走这里)"""
        ind = it.get("indicator", "")
        if not ind:
            errors.append(f"第 {i + 1} 个指标缺少 indicator 键")
        elif known_indicators is not None and ind not in known_indicators:
            errors.append(f"指标 {ind} 不在数据目录中(无 indicator 声明)")
        t = str(it.get("transform") or "").lower()
        if t and t not in TRANSFORMS:
            errors.append(f"指标 {ind or i + 1} 的 transform 非法: {t}")

    @staticmethod
    def _validate_code_item(it: dict, i: int,
                            known_code_sources: dict[str, dict] | None,
                            errors: list[str]) -> None:
        """校验代码类条目"""
        table = it.get("table", "")
        code_col = it.get("code_column", "")
        code = it.get("code", "")
        value_col = it.get("value_column", "")
        label = code or table or i + 1
        missing = [f for f, v in
                   (("table", table), ("code_column", code_col),
                    ("code", code), ("value_column", value_col)) if not v]
        if missing:
            errors.append(f"第 {i + 1} 个代码条目缺少 {'/'.join(missing)}")
            return
        if value_col not in CODE_VALUE_COLUMNS:
            errors.append(f"代码条目 {label} 的 value_column 非法: {value_col}")
        if known_code_sources is not None:
            src = known_code_sources.get(table)
            if src is None:
                errors.append(f"表 {table} 不是可用的代码类数据源")
            else:
                if src.get("deprecated"):
                    errors.append(f"表 {table} 已停更(deprecated), 不可加入主题")
                if code_col != src.get("code_column"):
                    errors.append(
                        f"表 {table} 的代码列应为 {src.get('code_column')}, 当前 {code_col}")


def _parse_date(value) -> date | None:
    """宽松解析日期字符串:YYYY-MM-DD / YYYYMMDD / YYYY/MM/DD;失败 → None"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# BoardService — 数据组装
# ---------------------------------------------------------------------------


class BoardService:
    """把主题组装成 DataFrame(纯查询,无状态;GUI 与 API 共用)

    复用 repo.get_indicator + compute_transform:
        get_indicator 统一返回 {date, value}(中文月份/季度列已归一化);
        compute_transform 做 level/yoy/mom 派生(频率自动推断)。
    """

    def __init__(self, repo):
        self.repo = repo

    # ---- 快照概览 ----

    def snapshot(self, board: dict, transform: str | None = None) -> pd.DataFrame:
        """每日快照:每指标一行 [指标, 最新日期, 最新值, 环比%, 同比%, 近3期]

        - 最新值:按指标有效口径(item.transform → unit_type → level)的最新值;
          transform 参数可全局覆盖(API ?transform=)
        - 环比%/同比%:无论主口径,恒给出派生 mom/yoy
        - 近3期:时间顺序最近 3 个原始值(逗号分隔)
        """
        rows = []
        for it in board.get("items", []):
            if not isinstance(it, dict):
                continue
            if _item_type(it) == "code":
                row = self._snapshot_code_row(it, board)
                if row:
                    rows.append(row)
                continue
            ind = it.get("indicator", "")
            if not ind:
                continue
            start, end = _item_dates(it, board)
            level = self.repo.get_indicator(
                ind, start_date=start, end_date=end)
            if level.empty:
                continue

            label = str(it.get("name") or ind)
            eff = self._effective_transform(it, ind, transform)
            # 主口径值(快照「最新值」列)
            main = compute_transform(level, eff)
            # 派生列(mom/yoy)只在存储口径为 level 时才算——中国官方源存储值本身
            # 就是同比%(unit_type=yoy), 再派生就是"对增长率求增长率", 无意义。
            # 这类源环比/同比列留空, 主口径值即原始存储值(如 cn.cpi 1.0)。
            stored_unit = self._stored_unit(ind)
            mom = compute_transform(level, "mom") if stored_unit == "level" else None
            yoy = compute_transform(level, "yoy") if stored_unit == "level" else None

            rows.append({
                "类型": "指标",
                "指标": label,
                "指标键": ind,
                "最新日期": _fmt_date(level.iloc[0]["date"]),
                "最新值": _fmt_num(_latest(main)),
                "环比%": _fmt_num(_latest(mom)),
                "同比%": _fmt_num(_latest(yoy)),
                "近3期": _last3(level),
            })
        if not rows:
            return pd.DataFrame(columns=[
                "类型", "指标", "指标键", "最新日期", "最新值",
                "环比%", "同比%", "近3期"])
        return pd.DataFrame(rows)

    # ---- 代码类条目(基金/股票/指数日线) ----

    def _code_series(self, item: dict, board: dict) -> pd.DataFrame:
        """代码条目查询: repo.query(table, filters={code_col: code}) → {date, value} 降序

        复用 repo.query 的 filters 等值过滤 + 日期区间 + LIMIT 倒序。不传 limit
        (单 code 完整窗口, 量级 = 该 code 行数, 保证近3期有足够前值)。
        日期列对 fund/stock/index 都是 ISO date, normalize_cn_date_str 兜底中文列。
        """
        table = item.get("table", "")
        code_col = item.get("code_column", "")
        code = item.get("code", "")
        value_col = item.get("value_column", "") or "close"
        if not table or not code_col or not code:
            return pd.DataFrame(columns=["date", "value"])
        start, end = _item_dates(item, board)
        try:
            df = self.repo.query(
                table, filters={code_col: code},
                date_from=start, date_to=end)
        except Exception as exc:
            logger.warning("主题代码条目查询失败 %s:%s: %s", table, code, exc)
            return pd.DataFrame(columns=["date", "value"])
        if df.empty or value_col not in df.columns:
            return pd.DataFrame(columns=["date", "value"])
        date_col = item.get("date_column") or self._find_date_col_in_df(df) or "date"
        if date_col not in df.columns:
            return pd.DataFrame(columns=["date", "value"])
        out = pd.DataFrame({
            "date": df[date_col].map(normalize_cn_date_str),
            "value": df[value_col],
        })
        return out.sort_values("date", ascending=False).reset_index(drop=True)

    @staticmethod
    def _find_date_col_in_df(df) -> str | None:
        """在查询结果 DataFrame 里定位日期列"""
        for c in ("date", "交易日期", "时间", "日期", "trade_date", "datetime"):
            if c in df.columns:
                return c
        return None

    def _snapshot_code_row(self, it: dict, board: dict) -> dict | None:
        """代码条目的快照行: 最新值 = 值列(close)最新一条, 环比/同比留空"""
        df = self._code_series(it, board)
        if df.empty:
            return None
        table = it.get("table", "")
        code = it.get("code", "")
        label = str(it.get("name") or f"{table}:{code}")
        return {
            "类型": "代码",
            "指标": label,
            "指标键": f"{table}:{code}",
            "最新日期": _fmt_date(df.iloc[0]["date"]),
            "最新值": _fmt_num(df.iloc[0]["value"]),
            "环比%": None,  # 价格衍生意义弱, 后置(series 仍可 ?transform=)
            "同比%": None,
            "近3期": _last3(df),
        }

    # ---- 时间序列(宽表) ----

    def series(self, board: dict, start_date: str | None = None,
               end_date: str | None = None,
               transform: str | None = None) -> pd.DataFrame:
        """全部指标按日期对齐成宽表:date 为行、每指标一列

        日期窗口:显式 start_date/end_date 优先,其次 item/board 级配置。
        transform 传了则覆盖所有 item 的口径;否则各自按有效口径。
        """
        frames: list[pd.DataFrame] = []
        for it in board.get("items", []):
            if not isinstance(it, dict):
                continue
            s, e = _item_dates(it, board)
            s = start_date or s
            e = end_date or e
            if _item_type(it) == "code":
                df = self._code_series(it, board)
                if df.empty:
                    continue
                eff = transform or "level"  # 代码条目无 item 级 transform, 只认全局覆盖
                df = compute_transform(df, eff)
                label = _unique_label(it, f"{it.get('table')}:{it.get('code')}", frames)
            else:
                ind = it.get("indicator", "")
                if not ind:
                    continue
                df = self.repo.get_indicator(ind, start_date=s, end_date=e)
                if df.empty:
                    continue
                eff = transform or self._effective_transform(it, ind)
                df = compute_transform(df, eff)
                label = _unique_label(it, ind, frames)
            out = pd.DataFrame({
                "date": pd.to_datetime(df["date"], errors="coerce"),
                label: pd.to_numeric(df["value"], errors="coerce"),
            }).dropna(subset=["date"])
            if not out.empty:
                frames.append(out)

        if not frames:
            return pd.DataFrame(columns=["date"])
        merged = frames[0]
        for df in frames[1:]:
            merged = merged.merge(df, on="date", how="outer")
        merged = merged.sort_values("date").reset_index(drop=True)
        merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
        return merged

    # ---- 辅助 ----

    def _stored_unit(self, indicator_key: str) -> str:
        """获信源存储口径(meta_indicator.unit_type), 缺省 level"""
        try:
            mapping = self.repo.get_indicator_map(indicator_key)
            ut = str((mapping or {}).get("unit_type") or "level").lower()
            return ut if ut in TRANSFORMS else "level"
        except Exception:
            return "level"

    def _effective_transform(self, item: dict, indicator_key: str,
                             override: str | None = None) -> str:
        """指标有效口径:override → item.transform → meta_indicator.unit_type → level"""
        if override:
            t = str(override).strip().lower()
            if t in TRANSFORMS:
                return t
        t = str(item.get("transform") or "").strip().lower()
        if t in TRANSFORMS:
            return t
        return self._stored_unit(indicator_key)


def _item_dates(item: dict, board: dict) -> tuple[str | None, str | None]:
    """item 级日期覆盖 board 级(null 继承);「始终最新」= 均 None"""
    start = item.get("date_start")
    if start in (None, ""):
        start = board.get("date_start")
    end = item.get("date_end")
    if end in (None, ""):
        end = board.get("date_end")
    return _norm(start), _norm(end)


def _norm(value):
    """日期字段归一化为 YYYYMMDD(repo.get_indicator 两格式都认,统一一种)"""
    if value in (None, ""):
        return None
    d = _parse_date(value)
    return d.strftime("%Y%m%d") if d else None


def _fmt_date(value) -> str:
    """日期值 → YYYY-MM-DD(兼容 Timestamp/date/字符串)"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        return str(pd.Timestamp(value).date())
    except Exception:
        return str(value)[:10]


def _fmt_num(value) -> float | None:
    """数值 → float(保留2位);NaN/None → None(JSON 安全)"""
    if value is None:
        return None
    try:
        v = float(value)
        return None if pd.isna(v) else round(v, 2)
    except (TypeError, ValueError):
        return None


def _latest(df: pd.DataFrame) -> float | None:
    """派生序列的最新值(compute_transform 输出已升序,取最后一行)"""
    if df is None or df.empty:
        return None
    return df.iloc[-1]["value"]


def _last3(level: pd.DataFrame) -> str:
    """近3期原始值(时间顺序),逗号分隔;空 → 空串

    get_indicator 返回降序(最新在前)→ 取 head(3) 是最近3期, 反转成时间顺序。
    """
    vals = pd.to_numeric(level["value"], errors="coerce").dropna()
    if vals.empty:
        return ""
    tail = vals.head(3).iloc[::-1]
    return ", ".join(f"{v:.2f}" for v in tail)


def _unique_label(item: dict, indicator_key: str, frames: list[pd.DataFrame]) -> str:
    """宽表列名:显示名优先,冲突(多指标同名/同键)自动加序号"""
    base = str(item.get("name") or indicator_key).strip() or indicator_key
    label = base
    taken = set()
    for f in frames:
        taken.update(f.columns)
    n = 2
    while label in taken:
        label = f"{base}({n})"
        n += 1
    return label
