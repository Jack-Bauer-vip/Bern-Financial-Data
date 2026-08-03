"""目标表智能识别 — 规则匹配优先，AI 兜底

规则匹配：根据文件列名与表列名的重叠度、日期列匹配、样本值类型，
对所有候选表评分排序。置信度低于阈值时调 AI 兜底。

设计目标：优先可离线、可预测的规则；AI 只在规则拿不准时介入，
且 AI 不可用（超时/无服务）时能安全降级回规则结果。
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.utils.config import ConfigManager

# 日期类列名（大小写不敏感，与 repository.DATE_COLUMN_KEYWORDS 对齐）
_DATE_KEYWORDS = (
    "date", "时间", "日期", "月份", "trade_date", "datetime",
    "发布日期", "报告期",
)

# 归一化时忽略的字符
_NORM_RE = re.compile(r"[^a-z0-9一-鿿]")


def normalize_col(name: str) -> str:
    """列名归一化：去 BOM/空白/非字母数字/中文符号，转小写"""
    return _NORM_RE.sub("", str(name).strip().lstrip("﻿").lower())


def is_date_col(name: str) -> bool:
    """判断列名是否日期类"""
    n = normalize_col(name)
    return any(kw in n for kw in _DATE_KEYWORDS)


def sample_dtype(df: pd.DataFrame, col: str) -> str:
    """推断样本列的类型：date / numeric / text

    顺序：数值 dtype / 全数字字符串 → 先判数值，
    再判日期（字符串日期），避免 pandas 把 float 当纳秒时间戳。
    """
    try:
        series = df[col].dropna()
    except Exception:
        return "text"
    if series.empty:
        return "text"
    sample = series.head(50)

    # 1. 数值 dtype（int/float）或全数字字符串
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    try:
        pd.to_numeric(sample, errors="raise")
        return "numeric"
    except Exception:
        pass

    # 2. 日期字符串（排除纯数字）
    try:
        pd.to_datetime(sample, errors="raise")
        return "date"
    except Exception:
        return "text"


@dataclass
class TableMeta:
    """候选表元信息"""
    table_name: str
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    display_name: str = ""  # 数据源中文名，帮助 AI 区分同构表


@dataclass
class MatchResult:
    """匹配结果"""
    table_name: str
    confidence: float
    method: str  # "rules" | "ai"
    reason: str = ""
    is_ai: bool = False


class RuleMatcher:
    """基于规则的候选表匹配器"""

    def __init__(self, tables: list[TableMeta]):
        self.tables = tables

    # ------------------------------------------------------------------
    # 列名相似度
    # ------------------------------------------------------------------

    @staticmethod
    def _jaccard(set_a: set, set_b: set) -> float:
        """Jaccard 相似度"""
        if not set_a or not set_b:
            return 0.0
        inter = len(set_a & set_b)
        union = len(set_a | set_b)
        return inter / union if union else 0.0

    def _column_overlap_score(self, file_cols: list[str], table_cols: list[str]) -> float:
        """文件列与表列的归一化重叠度

        - 精确匹配（归一化后相同）权重高
        - 日期类列单独加分（表通常有且仅有一个日期列）
        """
        file_norm = {normalize_col(c) for c in file_cols}
        table_norm = {normalize_col(c) for c in table_cols}
        if not file_norm:
            return 0.0

        exact = len(file_norm & table_norm)
        # 日期列识别加分：文件与表都有日期列 → +0.2
        date_bonus = 0.0
        if any(is_date_col(c) for c in file_cols) and any(is_date_col(c) for c in table_cols):
            date_bonus = 0.2

        # 重叠比例：重叠的列数 / 文件列数（越少的文件列被遗漏越好）
        coverage = exact / len(file_norm)
        # Jaccard：抑制表列很多但都无关的情况
        jaccard = self._jaccard(file_norm, table_norm)

        score = 0.5 * coverage + 0.3 * jaccard + date_bonus
        return min(score, 1.0)

    def _sample_type_score(self, df: pd.DataFrame, table_cols: list[str]) -> float:
        """样本值类型匹配度：文件日期列和数值列是否能在表列中找到对应

        表里有日期列 + 文件有日期列 → 各加 0.1。
        """
        if df is None or df.empty:
            return 0.0
        file_date = any(is_date_col(c) for c in df.columns)
        file_num = any(sample_dtype(df, c) == "numeric" for c in df.columns)
        table_date = any(is_date_col(c) for c in table_cols)

        score = 0.0
        if file_date and table_date:
            score += 0.15
        # 数值列通常表里也有（非精确匹配也能给启发）
        if file_num:
            score += 0.05
        return min(score, 0.2)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def score_all(
        self, df: pd.DataFrame, exclude: str | None = None
    ) -> list[tuple[TableMeta, float]]:
        """对所有候选表评分，返回 (表, 置信度) 降序列表"""
        file_cols = list(df.columns) if df is not None else []
        scored = []
        for meta in self.tables:
            if exclude and meta.table_name == exclude:
                continue
            col_score = self._column_overlap_score(file_cols, meta.columns)
            type_score = self._sample_type_score(df, meta.columns)
            # 表有数据 + 小加成（已有数据的表更可能是目标）
            data_bonus = 0.03 if meta.row_count > 0 else 0.0
            total = col_score + type_score + data_bonus
            scored.append((meta, round(min(total, 1.0), 3)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def best(
        self, df: pd.DataFrame, exclude: str | None = None, threshold: float = 0.7
    ) -> tuple[MatchResult | None, list[tuple[TableMeta, float]]]:
        """返回最佳匹配（若置信度达标且无歧义）与全部评分

        三种结果：
        - 返回 MatchResult(method="rules")：置信度达标且与次高明显拉开
        - 返回 None 且 _ambiguity=True：规则拿不准（置信度不足 或 并列歧义），
          上层应调 AI 兜底，AI 不可用时可用 scored[0] 退回
        - 返回 None 且无候选：无表可匹配
        """
        scored = self.score_all(df, exclude)
        self._ambiguity = False
        if not scored:
            return None, scored

        top, conf = scored[0]
        if conf < threshold:
            self._ambiguity = True
            return None, scored

        # 并列歧义：top1 与 top2 差距过小（如同结构的 20 张宏观表）
        if len(scored) > 1:
            _, second_conf = scored[1]
            if conf - second_conf < 0.05:
                self._ambiguity = True
                return None, scored

        return MatchResult(
            table_name=top.table_name,
            confidence=conf,
            method="rules",
            reason=f"列名匹配 {conf:.0%}",
        ), scored


def collect_tables(repo) -> list[TableMeta]:
    """从仓库收集所有候选表元信息（数据表，排除 meta_ 前缀）"""
    from src.db.repository import DataRepository
    from sqlalchemy import text

    if not isinstance(repo, DataRepository):
        return []

    tables: list[TableMeta] = []
    # 建立 table_name → 中文名 映射（来自 data_catalog.yaml，帮助 AI 区分同构表）
    name_map: dict[str, str] = {}
    try:
        for src in repo.get_all_enabled_sources():
            tn = src.get("table_name")
            if tn and src.get("name"):
                name_map[tn] = src.get("name")
    except Exception:
        pass

    try:
        with repo.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT GLOB 'meta_*' "
                "AND name != 'sqlite_sequence'"
            )).fetchall()
            for (tname,) in rows:
                cols = repo.get_all_existing_columns(tname)
                if not cols:
                    continue
                try:
                    cnt = conn.execute(
                        text(f"SELECT COUNT(*) FROM {repo._quote_table(tname)}")
                    ).scalar() or 0
                except Exception:
                    cnt = 0
                tables.append(TableMeta(
                    tname, cols, cnt, name_map.get(tname, "")))
    except Exception:
        pass
    return tables


def build_tables_desc(tables: list[TableMeta], limit: int = 30) -> str:
    """为 AI 生成表清单描述文本（表名: 中文名 + 列名列表）"""
    lines = []
    for meta in tables[:limit]:
        cols = ", ".join(meta.columns[:12])
        display = f" ({meta.display_name})" if meta.display_name else ""
        lines.append(f"- {meta.table_name}{display}: {cols}")
    return "\n".join(lines)


def sample_values(df: pd.DataFrame, max_cols: int = 12) -> dict:
    """取文件列的前几个样本值（转字符串，供 AI 识别用）"""
    if df is None or df.empty:
        return {}
    out = {}
    for col in list(df.columns)[:max_cols]:
        vals = [str(v) for v in df[col].dropna().head(3).tolist()]
        if vals:
            out[col] = vals
    return out


# ---------------------------------------------------------------------------
# 基金/ETF 识别
#
# 用户的基金行情 CSV 通常是「每基金一个文件」（如 159001.csv），文件名含
# 6 位基金代码。识别出代码后直接路由到 fund_etf_daily 表并注入 code 列，
# 使同一基金的 CSV 上传与 API 同步按 (code, date) 合并去重。
# ---------------------------------------------------------------------------

# 基金数据目标表名（与 data_catalog.yaml 的 fund.etf_daily 节点一致）
FUND_TABLE = "fund_etf_daily"

# 基金行情表规范列（CSV 与 API 统一映射到这些列，保证合并）
FUND_CANONICAL_COLUMNS = (
    "date", "open", "high", "low", "close", "volume", "amount", "code",
)

# 基金/ETF 代码前缀（区分股票 000/300/600/688 等）
_FUND_CODE_PREFIXES = ("15", "16", "50", "51", "52", "56", "58", "59")


def detect_fund_code(filename: str) -> str | None:
    """从文件名提取 6 位基金代码

    例：159001.csv → '159001'；Data_518880.SH.csv → '518880'；
    2026-06-22ETF.csv → None（日期串不匹配前缀）。
    仅接受基金代码前缀，避免把股票代码（600519 等）误判为基金。
    """
    if not filename:
        return None
    base = os.path.basename(str(filename))
    for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", base):
        code = m.group(1)
        if code.startswith(_FUND_CODE_PREFIXES):
            return code
    return None


# ---------------------------------------------------------------------------
# 会话级识别缓存
#
# 批量导入上千个「同类型表头」文件时，每个文件都会重新跑一遍目标表识别
# （规则评分 + 规则拿不准时调 AI）。识别结果只依赖文件结构与候选表结构，
# 同表头文件可复用首次结果，避免重复 AI 调用。表结构变化时签名随之变化，
# 缓存自动失效。
# ---------------------------------------------------------------------------

_match_cache: dict[tuple, MatchResult] = {}
_MAX_CACHE_SIZE = 1000


def header_signature(df: pd.DataFrame) -> tuple[tuple[str, str], ...]:
    """文件结构签名：每列 (归一化列名, 样本类型 date/numeric/text)，排序后元组

    同表头文件签名相同 → 复用上次识别结果；样本类型用于区分同名不同型数据。
    """
    if df is None or df.empty:
        return ()
    return tuple(
        sorted((normalize_col(c), sample_dtype(df, c)) for c in df.columns)
    )


def tables_signature(tables: list[TableMeta]) -> tuple:
    """候选表结构签名：表名 + 列 + 是否有数据（>0 粗粒度，避免同步频繁失效）"""
    return tuple(
        sorted(
            (t.table_name, tuple(sorted(t.columns)), t.row_count > 0)
            for t in tables
        )
    )


def clear_identification_cache() -> None:
    """清空识别/列映射的会话缓存（测试与维护用）"""
    _match_cache.clear()
    from src.importer import column_mapper
    column_mapper.clear_mapping_cache()


def match_table(
    df: pd.DataFrame,
    repo,
    ai_client=None,
    exclude: str | None = None,
    threshold: float | None = None,
    tables_meta: list[TableMeta] | None = None,
    filename: str | None = None,
) -> MatchResult:
    """目标表识别入口：规则优先，AI 兜底，AI 不可用降级回规则

    Parameters
    ----------
    tables_meta : list[TableMeta] | None
        预收集的候选表（避免重复查库）；为空时内部 collect_tables(repo)
    filename : str | None
        文件名（暂用于日志/提示，规则评分可后续扩展文件名加分）

    Returns:
        MatchResult（table_name 可能为空字符串表示无匹配）
    """
    if threshold is None:
        config = ConfigManager()
        threshold = float(config.get("ai.rule_threshold", 0.7))

    tables = tables_meta if tables_meta else collect_tables(repo)
    if not tables:
        return MatchResult("", 0.0, "rules", reason="无候选表")

    # 会话级缓存：同表头 + 同候选表结构 + 是否可用 AI → 复用首次识别结果。
    # 键含 ai 可用性：有无 AI 时结果不同（规则 vs AI 兜底），分开缓存避免串用。
    key = (header_signature(df), tables_signature(tables), threshold, exclude,
           ai_client is not None)
    cached = _match_cache.get(key)
    if cached is not None:
        return cached

    result = _match_table_compute(df, ai_client, exclude, threshold, tables)
    if len(_match_cache) >= _MAX_CACHE_SIZE:
        _match_cache.clear()
    _match_cache[key] = result
    return result


def _match_table_compute(
    df: pd.DataFrame,
    ai_client,
    exclude: str | None,
    threshold: float,
    tables: list[TableMeta],
) -> MatchResult:
    """实际识别计算（规则优先，AI 兜底，AI 不可用降级回规则）"""
    matcher = RuleMatcher(tables)
    res, scored = matcher.best(df, exclude=exclude, threshold=threshold)

    # 规则已明确 → 直接用
    if res is not None:
        return res

    # 规则拿不准 → AI 兜底
    valid_tables = {t.table_name for t in tables}
    if ai_client is not None and ai_client.is_available():
        ai_res = ai_client.identify_table(
            list(df.columns) if df is not None else [],
            sample_values(df),
            build_tables_desc(tables),
        )
        # ★ AI 返回的表名必须在校验集合内（防注入/防幻觉）
        if ai_res and ai_res.get("table") in valid_tables:
            return MatchResult(
                table_name=str(ai_res["table"]),
                confidence=float(ai_res.get("confidence", 0.5)),
                method="ai",
                reason=ai_res.get("reason", ""),
                is_ai=True,
            )

    # AI 不可用 / 无结果 → 退回规则 top1（低置信）
    if scored:
        top, conf = scored[0]
        return MatchResult(
            table_name=top.table_name,
            confidence=conf,
            method="rules",
            reason="规则低置信，AI 不可用，退回最佳候选",
        )

    return MatchResult("", 0.0, "rules", reason="无匹配")
