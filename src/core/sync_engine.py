"""增量同步引擎 — 核心数据流水线"""

import random
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from PySide6.QtCore import QObject, Signal

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.exceptions import BernError, DataFetchError
from src.db.repository import DataRepository
from src.utils.config import ConfigManager
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# 每数据源互斥锁 —— 防止同一数据源被并发同步
# GUI 手动同步 / API 触发 / 定时调度可能各持独立 SyncEngine 实例，
# 单靠实例内 _running 无法互斥，需进程内按 table_name 加锁。
# ---------------------------------------------------------------------------

_sync_locks: dict[str, threading.Lock] = {}
_sync_locks_guard = threading.Lock()


def _get_sync_lock(key: str) -> threading.Lock:
    """按 key（数据表名）返回进程内共享的互斥锁（惰性创建）"""
    with _sync_locks_guard:
        lock = _sync_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _sync_locks[key] = lock
        return lock


def _is_source_busy(table_name: str) -> bool:
    """该数据表是否正在同步（供 API 层在起线程前做快速判断）"""
    with _sync_locks_guard:
        lock = _sync_locks.get(table_name)
    return lock is not None and lock.locked()


def normalize_ts_code(code: str) -> str:
    """归一化 tushare 代码：有后缀则大写归一，纯 6 位则按前缀推断交易所。

    159001 → 159001.SZ；510050 → 510050.SH；159001.sz → 159001.SZ。
    """
    code = (code or "").strip().upper()
    if not code:
        return ""
    if re.fullmatch(r"\d{6}\.(SZ|SH|BJ)", code):
        return code
    if re.fullmatch(r"\d{6}", code):
        # 5/6 开头多为上交所，其余（15/16/4 等）多为深交所
        return code + (".SH" if code[0] in "56" else ".SZ")
    return code


def _strip_exchange(code: str) -> str:
    """去掉 tushare 代码的交易所后缀：159001.SZ → 159001"""
    return re.sub(r"\.(SZ|SH|BJ)$", "", (code or "").strip(), flags=re.I)


# ---------------------------------------------------------------------------
# 行情列规范化
#
# akshare 行情接口返回中文列（日期/开盘/收盘/...），而 CSV 导入写入的是规范
# 英文列（date/open/... + code/symbol）。为让 API 更新与 CSV 上传合并到同一表、
# 并按 (code,date)/(symbol,date) 去重，同步前统一列名并注入代码列。
# column_map 从 data_catalog.yaml 的节点配置读取（无则用 fund 兜底映射）。
# ---------------------------------------------------------------------------

FUND_TABLE = "fund_etf_daily"

# fund 兜底列映射（data_catalog.yaml 的 fund.etf_daily 未配 column_map 时用）
_FUND_COLUMN_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close",
    "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
}

_FUND_KEEP_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")

# 规范列优先顺序（重排用；多余的保持原序追加）
_PREFERRED_CANONICAL = ("date", "open", "high", "low", "close", "volume",
                        "amount", "code", "symbol")


def _apply_column_map(
    df: pd.DataFrame,
    column_map: dict | None,
    code: str,
    code_col: str,
) -> pd.DataFrame:
    """按 column_map 重命名列并注入代码列

    column_map : 源列 → 规范列（None/空则只做代码列注入）
    code       : 注入的代码值（为空则不注入）
    code_col   : 注入到哪个列（"symbol" 或 "code"）
    """
    if df is None or df.empty:
        return df
    df = df.rename(columns=column_map or {})
    if code and code_col and code_col not in df.columns:
        df[code_col] = code
    # 声明了 column_map 时只保留映射到的规范列 + 代码列，丢弃未映射的额外列
    # （如振幅/涨跌幅/换手率），并重排为规范列顺序，保持本地表列结构整洁
    if column_map:
        keep = set(column_map.values())
        if code_col:
            keep.add(code_col)
        drop = [c for c in df.columns if c not in keep]
        if drop:
            df = df.drop(columns=drop)
        ordered = [c for c in _PREFERRED_CANONICAL if c in df.columns]
        ordered += [c for c in df.columns if c not in ordered]
        df = df[ordered]
    return df


def _canonicalize_fund_df(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """基金行情结果规范化为 date/open/high/low/close/volume/amount/code（兼容旧调用）"""
    return _apply_column_map(df, _FUND_COLUMN_MAP, code, "code")


class SyncEngine(QObject):
    """增量同步引擎，协调获取、清洗、写入全流程

    Signals
    -------
    sync_started(source_key: str)
        开始同步某数据源时触发
    sync_progress(source_key: str, current: int, total: int)
        同步进度更新
    sync_completed(source_key: str, added_rows: int)
        同步完成，返回写入行数
    sync_error(source_key: str, error_msg: str)
        同步过程中发生错误
    log_message(level: str, message: str)
        内部日志消息（用于 GUI 日志控件）
    """

    sync_started = Signal(str)
    sync_progress = Signal(str, int, int)
    sync_completed = Signal(str, int)
    sync_error = Signal(str, str)
    log_message = Signal(str, str)

    def __init__(
        self,
        fetcher: DataFetcher,
        repo: DataRepository,
        schema_mgr: DynamicSchemaManager,
        config: ConfigManager,
    ):
        super().__init__()
        self.fetcher = fetcher
        self.repo = repo
        self.schema_mgr = schema_mgr
        self.config = config
        self._running = False
        self._stop_requested = False

    # ------------------------------------------------------------------
    # 运行控制
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """请求停止后续同步（设置停止标志）"""
        self._stop_requested = True
        self._log("INFO", "同步引擎收到停止请求")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # 单数据源同步
    # ------------------------------------------------------------------

    def run(
        self,
        source_key: str,
        history_years: int = 3,
        full_refresh: bool = False,
        params_override: dict | None = None,
    ) -> int | None:
        """执行单个数据源的增量同步

        Parameters
        ----------
        source_key : str
            data_catalog.yaml 中定义的数据源唯一标识
        history_years : int
            首次同步时拉取的历史年数，默认 3 年
        full_refresh : bool
            是否全部重新拉取（忽略 last_sync_date），默认 False
        params_override : dict | None
            覆盖默认参数（如 GUI 用户输入的 symbol/code），优先于 params_template.default

        Returns
        -------
        int | None
            本次写入行数，失败返回 None
        """
        # 共享 SyncEngine 实例（GUI 用）重入守卫
        if self._running:
            self._log("WARNING", "同步引擎正在运行中，跳过本次请求")
            return 0
        self._running = True
        self._stop_requested = False

        # 预置错误处理器依赖的变量 —— source_cfg 缺失时（下方 raise）也会进入
        # except 分支引用它们，若不预置会在错误处理器里抛 NameError 掩盖真实错误
        source_cfg: dict = {}
        table_name = source_key
        api_source = ""
        func_name = ""

        # 每数据源互斥锁（进程级，覆盖 GUI/API/调度各自独立的 SyncEngine 实例）
        sync_lock: threading.Lock | None = None
        lock_held = False

        try:
            # 1. 获取数据源配置
            source_cfg = self.repo.get_source_config(source_key)
            if source_cfg is None:
                raise DataFetchError(f"未找到数据源配置: {source_key}")

            table_name = source_cfg.get("table_name", source_key)
            func_name = source_cfg.get("api_function", "")
            api_source = source_cfg.get("api_source", "")

            # 该数据源已在同步中则跳过本次请求（非阻塞获取）
            sync_lock = _get_sync_lock(table_name)
            if not sync_lock.acquire(blocking=False):
                self._log("WARNING", f"[{source_key}] 该数据源正在同步中，跳过本次请求")
                self.sync_completed.emit(source_key, 0)
                return 0
            lock_held = True

            self._log("INFO", f"开始同步 [{source_key}] -> {table_name}")
            self.sync_started.emit(source_key)

            # 2. 查询上次同步状态
            sync_job = self.repo.get_sync_job(table_name)
            last_date: date | None = sync_job.last_sync_date if sync_job else None

            # 3. 构建请求参数
            params: dict[str, Any] = {}
            override = params_override or {}

            # ★ 注入代码类参数（指数/基金等需要 symbol/code/ts_code 的源）：
            #   params_override（GUI 用户输入）优先，其次 params_template 的 default，
            #   避免同步时缺代码静默拉错数据
            for pkey in ("symbol", "code", "ts_code"):
                if pkey in override and str(override.get(pkey, "")).strip():
                    params[pkey] = str(override[pkey]).strip()
                elif pkey in (source_cfg.get("params_template") or {}):
                    default = source_cfg["params_template"][pkey].get("default", "")
                    if default:
                        params[pkey] = str(default)

            # 代码值（写入 code/symbol 列用，需在 param_map 重命名前取到）。
            # ts_code 去交易所后缀（159001.SZ → 159001），与 CSV 导入的 code 对齐。
            code_value = str(params.get("code") or params.get("symbol") or params.get("ts_code") or "")
            code_value = _strip_exchange(code_value)
            code_col = source_cfg.get("code_column")
            if not code_col:
                code_col = ("symbol" if "symbol" in (source_cfg.get("params_template") or {})
                            else "code" if "code" in (source_cfg.get("params_template") or {})
                            else "")

            # 日期参数格式（tushare 用 %Y%m%d，akshare 默认 ISO %Y-%m-%d）
            date_fmt = source_cfg.get("date_format", "%Y-%m-%d")

            # 增量起点：有 code 时【无条件】用该 code 的实际数据最大日期作起点。
            # 表级 last_sync_date 是全局的（可能被其他 code 的同步更新抬高，如 159001
            # 同步后表级变 7-31，而 159003 只到 6-22），对单个 code 无意义——若用它
            # 会跳过该 code 该补的区间。表级仅在 code 无数据时兜底。
            start_ref = last_date
            if code_value and code_col:
                actual = self.repo.get_max_date(table_name, code_col, code_value)
                if actual:
                    try:
                        start_ref = date.fromisoformat(str(actual))
                    except ValueError:
                        pass

            if full_refresh or start_ref is None:
                # 首次或无历史 -> 回退到固定历史区间
                params["start_date"] = (date.today() - timedelta(days=365 * history_years)).strftime(date_fmt)
                params["end_date"] = date.today().strftime(date_fmt)
                self._log("INFO", f"全量模式: {params['start_date']} ~ {params['end_date']}")
            else:
                # 增量模式：从参考日期（实际数据 max 或 last_sync_date）的次日起
                start = start_ref + timedelta(days=1)
                if start >= date.today():
                    self._log("INFO",
                              f"[{source_key}] {code_value or ''} 数据已到 {start_ref.isoformat()}，无需增量同步")
                    self.sync_completed.emit(source_key, 0)
                    return 0
                params["start_date"] = start.strftime(date_fmt)
                params["end_date"] = date.today().strftime(date_fmt)
                self._log("INFO", f"增量模式: {params['start_date']} ~ {params['end_date']}")

            # 统一参数名映射（akshare 常用 start_date/end_date）
            # 若配置中指定了字段映射，则覆盖
            param_map = source_cfg.get("param_map", {})
            for old_k, new_k in param_map.items():
                if old_k in params:
                    params[new_k] = params.pop(old_k)

            # 4. 获取数据（带重试）
            self._log("INFO", f"正在拉取 {func_name} ...")
            df = self._fetch_with_retry(source_cfg, params, max_retries=3)

            if df is None or df.empty:
                self._log("INFO", f"[{source_key}] 无新数据")
                self.sync_completed.emit(source_key, 0)
                return 0

            # ★ 行情列规范化：按节点 column_map 统一中文列 → 规范英文列，并注入
            #   代码列（stock→symbol、fund→code、index→symbol）。使 API 更新与
            #   CSV 导入写入同一表、按 (code,date)/(symbol,date) 合并去重。
            #   code_col 已在上方按 code_column 配置 / API 参数名解析。
            column_map = source_cfg.get("column_map")
            if column_map or (code_value and code_col):
                df = _apply_column_map(df, column_map, code_value, code_col)

            self.sync_progress.emit(source_key, 0, len(df))
            self._log("INFO", f"获取到 {len(df)} 行原始数据")

            # 5. 动态扩展列
            all_columns = self.schema_mgr.ensure_columns(
                table_name,
                list(df.columns),
                source_api=f"{api_source}.{func_name}",
            )

            # 6. 数据清洗
            df = self._clean_data(df)

            # 7. 批量写入
            unique_cols = self._get_unique_cols(source_cfg)
            batch_size = self.config.get("sync.batch_size", 500)
            added = self.repo.bulk_upsert(
                table_name,
                df,
                unique_columns=unique_cols,
                batch_size=batch_size,
            )

            # 8. 更新同步任务状态
            max_date = self._extract_max_date(df)
            row_count = self.repo.count_rows(table_name)

            self.repo.update_sync_job(table_name, {
                "display_name": source_cfg.get("name", source_key),
                "category": source_cfg.get("category", ""),
                "api_source": api_source,
                "api_function": func_name,
                "last_sync_time": datetime.now(),
                "last_sync_date": max_date,
                "row_count": row_count,
                "status": "completed",
                "error_message": None,
                "enabled": True,
            })

            # 8.5 指标归一层：同步成功后自动沿用获信源——该指标未设获信源、
            #     且当前表能明确解析数值列（今值/现值/value 等）时，首个同步
            #     成功的源自动成为默认获信源；用户可随后在 GUI 手动覆盖。
            ind_key = source_cfg.get("indicator")
            if ind_key:
                try:
                    self.repo.auto_adopt_indicator(str(ind_key), table_name)
                except Exception:
                    pass  # 自动沿用失败不影响同步结果

            self._log("INFO", f"[{source_key}] 同步完成，新增 {added} 条")
            self.sync_completed.emit(source_key, added)
            return added

        except BernError as exc:
            err_msg = str(exc)
            self._log("ERROR", f"[{source_key}] 同步失败: {err_msg}")
            self.sync_error.emit(source_key, err_msg)
            self._update_job_error(table_name, source_cfg, err_msg, api_source, func_name)
            return None
        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            self._log("ERROR", f"[{source_key}] 未预期异常: {err_msg}")
            self.sync_error.emit(source_key, err_msg)
            self._update_job_error(
                table_name,
                source_cfg,
                err_msg,
                source_cfg.get("api_source", ""),
                source_cfg.get("api_function", ""),
            )
            return None
        finally:
            if sync_lock is not None and lock_held:
                sync_lock.release()
            self._running = False

    # ------------------------------------------------------------------
    # 批量同步
    # ------------------------------------------------------------------

    def run_all(self) -> None:
        """顺序执行所有启用的数据源同步"""
        self._running = True
        self._stop_requested = False

        sources = self.repo.get_all_enabled_sources()
        total = len(sources)
        self._log("INFO", f"开始全量同步，共 {total} 个数据源")

        for i, src in enumerate(sources, 1):
            if self._stop_requested:
                self._log("WARNING", "用户中断，停止后续同步")
                break

            key = src.get("source_key") or src.get("table_name", "")
            self.sync_progress.emit(key, i - 1, total)
            self.run(key)

            if i < total and not self._stop_requested:
                gap = random.uniform(1.0, 3.0)
                time.sleep(gap)

        self._log("INFO", "全量同步结束")
        self._running = False

    # ------------------------------------------------------------------
    # 内部：网络请求重试
    # ------------------------------------------------------------------

    def _fetch_with_retry(
        self,
        source_cfg: dict,
        params: dict | None,
        max_retries: int = 3,
    ) -> pd.DataFrame | None:
        """指数退避重试获取数据"""
        last_exc: Exception | None = None

        for attempt in range(max_retries):
            try:
                # 请求前随机延迟，降低并发压力
                time.sleep(random.uniform(0.5, 2.0))
                return self.fetcher.fetch(source_cfg, params)
            except BernError as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 2**attempt + random.uniform(0, 1.0)
                    self._log(
                        "WARNING",
                        f"重试 {attempt + 1}/{max_retries}: {exc} (等待 {wait:.1f}s)",
                    )
                    time.sleep(wait)
                else:
                    self._log("ERROR", f"已达最大重试次数 {max_retries}，放弃: {exc}")
                    raise
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    wait = 2**attempt + random.uniform(0, 1.0)
                    self._log(
                        "WARNING",
                        f"重试 {attempt + 1}/{max_retries}: {exc} (等待 {wait:.1f}s)",
                    )
                    time.sleep(wait)
                else:
                    self._log("ERROR", f"非预期异常已达最大重试次数: {exc}")
                    raise DataFetchError(str(exc)) from exc

        return None  # 不应到达此处

    # ------------------------------------------------------------------
    # 内部：唯一键推断
    # ------------------------------------------------------------------

    @staticmethod
    def _get_unique_cols(source_cfg: dict) -> list[str]:
        """根据表名启发式推断 upsert 唯一键（共享逻辑）"""
        from src.core.unique_key import infer_unique_cols
        return infer_unique_cols(source_cfg)

    # ------------------------------------------------------------------
    # 内部：数据清洗
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_cn_date_str(value) -> str:
        """把中文年月日字符串归一化为 pd.to_datetime 可解析的形式

        覆盖 akshare 常见格式：
        2008年01月 / 2008年1月 → 2008-01（年+月、无日）
        2008年01月15日 → 2008-01-15
        非字符串 / 无中文日期字样的原样返回。
        """
        if not isinstance(value, str):
            return value
        s = value.strip()
        if not any(c in s for c in "年月日"):
            return s
        s = s.replace("年", "-").replace("月", "-").replace("日", "")
        # 去掉尾部残留分隔符（无日期的年月会留下 "2008-01-"）
        s = re.sub(r"[-/]+$", "", s).strip()
        return s

    @staticmethod
    def _clean_data(df: pd.DataFrame) -> pd.DataFrame:
        """标准化数据：删除全空行、转换日期列

        修复：akshare 某些宏观函数日期列是 "2008年01月"（年+月、无日），
        pd.to_datetime 解析不了会整列变 NaT，导致写库时 SQLite 无法绑定。
        这里先归一化中文年月日，再丢弃仍无法解析的行（避免 NaT 崩库）。
        """
        if df.empty:
            return df

        # 删除完全为空的行
        df = df.dropna(how="all")

        # 尝试识别并转换日期列
        date_keywords = ["date", "日期", "时间", "trade_date", "datetime"]
        for col in df.columns:
            col_lower = col.lower().strip()
            if any(kw in col_lower for kw in date_keywords):
                try:
                    # 先归一化中文年月日格式（akshare 常见 "2008年01月"）。
                    # 注意：pandas 3.x 字符串列是 str dtype 而非 object，须用
                    # is_string_dtype 判断，否则归一化被跳过 → 整列变 NaT。
                    if pd.api.types.is_string_dtype(df[col]):
                        df[col] = df[col].map(SyncEngine._normalize_cn_date_str)
                    df[col] = pd.to_datetime(df[col], errors="coerce")
                    # 若全部为 00:00:00 则统一为 date 类型（不含时间部分）
                    clean = df[col].dropna()
                    if not clean.empty and \
                            (clean.dt.time == pd.Timestamp("00:00:00").time()).all():
                        df[col] = df[col].dt.date
                except Exception:
                    pass  # 转换失败则保留原值

        # 防御：日期列仍有 NaT（无法解析/缺失）的行直接丢弃——
        #   数据表以日期为唯一键，无日期的行无法写入；也避免 NaT 绑参崩溃
        for col in df.columns:
            col_lower = col.lower().strip()
            if any(kw in col_lower for kw in date_keywords):
                na = df[col].isna()
                if na.any():
                    n = int(na.sum())
                    logger.warning("日期列 %s 有 %d 行无法解析/缺失，已丢弃", col, n)
                    df = df[~na]

        return df

    # ------------------------------------------------------------------
    # 内部：提取最大日期
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_max_date(df: pd.DataFrame) -> date | None:
        """从 DataFrame 中自动检测日期列并提取最大日期"""
        date_candidates = ["date", "日期", "时间", "trade_date", "datetime", "end_date", "start_date"]
        for col in date_candidates:
            if col in df.columns and not df[col].isna().all():
                try:
                    # 若已是 date 类型
                    if pd.api.types.is_datetime64_any_dtype(df[col]):
                        return df[col].max().date()
                    if pd.api.types.is_object_dtype(df[col]) or \
                            pd.api.types.is_string_dtype(df[col]):
                        parsed = pd.to_datetime(df[col], errors="coerce")
                        if parsed.notna().any():
                            return parsed.max().date()
                except Exception:
                    continue
        # 找不到可解析的日期列 —— 回退为 None 而非 today：
        # 若回退 today 会误写 last_sync_date=today，导致后续增量同步被永久跳过
        # （数据源静默停更）。返回 None 让下次同步回到全量模式重新拉取。
        logger.warning("数据中未找到可识别的日期列，last_sync_date 置空（下次将全量同步）")
        return None

    # ------------------------------------------------------------------
    # 内部：错误状态回写
    # ------------------------------------------------------------------

    def _update_job_error(
        self,
        table_name: str,
        source_cfg: dict,
        error_msg: str,
        api_source: str,
        api_function: str,
    ) -> None:
        """同步失败时将任务状态回写为 error"""
        try:
            self.repo.update_sync_job(table_name, {
                "display_name": source_cfg.get("name", table_name),
                "category": source_cfg.get("category", ""),
                "api_source": api_source,
                "api_function": api_function,
                "last_sync_time": datetime.now(),
                "status": "error",
                "error_message": error_msg[:500],
                "enabled": True,
            })
        except Exception as exc:
            logger.error("回写同步错误状态失败: %s", exc)

    # ------------------------------------------------------------------
    # 内部：日志
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        """统一日志：同时写入 logger 和 Qt 信号"""
        level_upper = level.upper()
        if level_upper == "ERROR":
            logger.error(message)
        elif level_upper == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)
        self.log_message.emit(level_upper, message)
