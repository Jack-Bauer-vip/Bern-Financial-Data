"""列名智能映射 + 新字段检测 — 规则优先，AI 兜底

把导入文件的列名映射到目标表的列名：
- 规则：中英常见金融列名对照表 + 日期列名归一化
- 新字段：映射不上的文件列 → 建议是否添加到表
"""

import re
from typing import Optional

import pandas as pd

# 中英常见金融列名对照（文件列名 → 目标表列名候选）
# 归一化后匹配：key 与 value 都是常见列名
COLUMN_ALIASES: dict[str, list[str]] = {
    # 日期类
    "时间": ["日期", "date", "trade_date", "发布日期", "报告期", "月份"],
    "日期": ["date", "trade_date", "时间", "发布日期", "报告期", "月份"],
    "月份": ["date", "日期", "时间", "trade_date"],
    "date": ["日期", "时间", "日期", "trade_date", "发布日期"],
    "trade_time": ["date", "日期", "trade_date"],
    "trade_date": ["日期", "时间", "date"],
    "发布日期": ["日期", "时间", "date"],
    "报告期": ["日期", "时间", "date"],
    # 行情类
    "开盘": ["open", "今开"],
    "open": ["开盘", "今开"],
    "收盘": ["close", "今收"],
    "close": ["收盘", "今收"],
    "最高": ["high", "最高价"],
    "high": ["最高", "最高价"],
    "最低": ["low", "最低价"],
    "low": ["最低", "最低价"],
    "成交量": ["volume", "成交量"],
    "volume": ["成交量"],
    "vol": ["volume", "成交量"],
    "成交额": ["amount", "成交额"],
    "amount": ["成交额"],
    "涨跌幅": ["pct_chg", "涨跌幅"],
    # 宏观值类
    "今值": ["今值", "现值", "数值", "value", "当前值"],
    "现值": ["现值", "今值", "数值", "value"],
    "数值": ["今值", "现值", "value", "数值"],
    "value": ["今值", "现值", "数值"],
    "预测值": ["预测值", "预期值", "forecast"],
    "预期值": ["预测值", "forecast"],
    "前值": ["前值", "previous", "上期值"],
    "上期值": ["前值", "previous"],
    "商品": ["商品", "指标", "name", "项目"],
    "指标": ["商品", "指标", "name", "项目"],
    "项目": ["商品", "指标", "name"],
    # 代码类
    "代码": ["code", "symbol", "基金代码", "指数代码"],
    "symbol": ["代码", "symbol", "指数代码"],
    "code": ["代码", "code", "基金代码"],
}

_NORM_RE = re.compile(r"[^a-z0-9一-鿿]")


def normalize_col(name: str) -> str:
    """列名归一化（与 matcher 一致）"""
    return _NORM_RE.sub("", str(name).strip().lstrip("﻿").lower())


def suggest_mapping(
    file_columns: list[str], table_columns: list[str]
) -> dict[str, str]:
    """把文件列名映射到表列名（规则），返回 {文件列: 目标表列}

    规则：
    1. 精确匹配（归一化后相同）
    2. 别名对照表匹配
    3. 日期类模糊：文件日期列 → 表中唯一的日期列
    未匹配的列不出现（视为新字段候选）
    """
    table_norm = {normalize_col(c): c for c in table_columns}
    table_date_cols = [c for c in table_columns
                       if any(kw in normalize_col(c)
                              for kw in ("date", "时间", "日期", "月份", "发布日期", "报告期"))]

    mapping: dict[str, str] = {}
    for fcol in file_columns:
        fnorm = normalize_col(fcol)
        if not fnorm:
            continue
        # 1. 精确匹配
        if fnorm in table_norm:
            mapping[fcol] = table_norm[fnorm]
            continue
        # 2. 别名对照表
        aliases = COLUMN_ALIASES.get(fcol.strip()) or COLUMN_ALIASES.get(fnorm, [])
        if not aliases and fcol.strip() in COLUMN_ALIASES:
            aliases = COLUMN_ALIASES[fcol.strip()]
        if not aliases and fnorm in COLUMN_ALIASES:
            aliases = COLUMN_ALIASES[fnorm]
        # 别名元素也需归一化后再与表列匹配（如 pct_chg → pctchg）
        mapped = next(
            (table_norm[normalize_col(a)] for a in aliases
             if normalize_col(a) in table_norm),
            None,
        )
        if mapped:
            mapping[fcol] = mapped
            continue
        # 3. 日期类模糊 → 表中唯一日期列
        if any(kw in fnorm for kw in ("date", "时间", "日期", "月份", "发布日期", "报告期")):
            if len(table_date_cols) == 1:
                mapping[fcol] = table_date_cols[0]
                continue

    return mapping


def detect_new_columns(
    file_columns: list[str], table_columns: list[str], mapping: dict[str, str] | None = None
) -> list[str]:
    """检测文件中有、但目标表（经映射后）没有的列 → 新字段候选

    判断依据：文件列名是否已进入 mapping 的键（键=文件列名）。
    """
    mapping = mapping or suggest_mapping(file_columns, table_columns)
    return [c for c in file_columns if c not in mapping]


class ColumnMappingResult:
    """列映射结果：mapping（文件列→表列）+ new_columns（建议新增）+ skipped（丢弃）"""

    def __init__(self, mapping: dict[str, str] | None = None,
                 new_columns: list[str] | None = None,
                 low_confidence: bool = False):
        self.mapping = mapping or {}
        self.new_columns = new_columns or []
        self.skipped: list[str] = []
        self.low_confidence = low_confidence

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """重命名列 + 丢弃 skipped 列，返回待写入的 DataFrame"""
        if df is None or df.empty:
            return df
        keep = [c for c in df.columns if c not in self.skipped]
        out = df[keep].copy()
        return out.rename(columns=self.mapping)


# 会话级列映射缓存：同文件列 × 同目标表列 → 复用映射结果
# ai_client.map_columns 只依赖列名（无样本值），按列名缓存完全正确。
# 批量导入同表头文件时避免重复规则映射与 AI 调用。
_mapping_cache: dict[tuple, ColumnMappingResult] = {}
_MAX_MAPPING_CACHE = 1000


def clear_mapping_cache() -> None:
    """清空列映射会话缓存"""
    _mapping_cache.clear()


def map_columns(
    file_columns: list[str],
    table_columns: list[str],
    ai_client=None,
) -> ColumnMappingResult:
    """列映射组合入口：规则优先 → AI 兜底 → 新字段检测

    防冲突：同一表列被多个文件列映射时，只保留第一个。
    """
    # 会话级缓存：同文件列 + 同目标表列 → 复用首次映射结果
    key = (tuple(sorted(file_columns)), tuple(sorted(table_columns)),
           ai_client is not None)
    cached = _mapping_cache.get(key)
    if cached is not None:
        return cached

    result = _map_columns_compute(file_columns, table_columns, ai_client)
    if len(_mapping_cache) >= _MAX_MAPPING_CACHE:
        _mapping_cache.clear()
    _mapping_cache[key] = result
    return result


def _map_columns_compute(
    file_columns: list[str],
    table_columns: list[str],
    ai_client,
) -> ColumnMappingResult:
    """实际列映射计算（规则优先 → AI 兜底 → 新字段检测）"""
    result = ColumnMappingResult()

    # 1. 规则映射
    mapping = suggest_mapping(file_columns, table_columns)
    mapped_file = set(mapping.keys())
    used_table = set(mapping.values())

    # 2. 未映射列 → AI 兜底
    unmapped = [c for c in file_columns if c not in mapped_file]
    if unmapped and ai_client is not None and ai_client.is_available():
        ai_map = ai_client.map_columns(unmapped, table_columns)
        if isinstance(ai_map, dict):
            for fcol, tcol in ai_map.items():
                if not tcol:
                    continue
                if tcol in table_columns and tcol not in used_table:
                    mapping[fcol] = tcol
                    used_table.add(tcol)
                    result.low_confidence = True  # AI 映射置信度较低

    result.mapping = mapping

    # 3. 新字段检测（未进入 mapping 键集合的文件列）
    result.new_columns = [c for c in file_columns if c not in mapping]
    return result
