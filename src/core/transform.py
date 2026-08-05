# -*- coding: utf-8 -*-
"""指标派生视图 — 对 {date, value} 两列做 level/yoy/mom 变换

纯 pandas 实现，零 GUI/DB/API 依赖。用于 /api/v1/indicator 的 transform 参数：
- level : 原始值（TEXT 存储先转数值）
- yoy   : 同比百分比 = (当前 / 一年前 - 1) * 100
- mom   : 环比百分比 = (当前 / 上一期 - 1) * 100（季度数据即 QoQ）
- pct   : 环比百分比别名（= mom，保持 API 面 yoy/mom/pct 一致）

频率由日期序列中位间隔推断（日/周/月/季/年 → yoy 的 shift 参数）；
不规则/缺失序列用位置 shift 兜底，调用方可传 freq 强指定。
"""

from typing import Any

import numpy as np
import pandas as pd

TRANSFORMS = {"level", "yoy", "mom", "pct"}

# 推断频率 → yoy 的 periods（pandas shift 用）
_FREQ_SHIFTS = {"D": 365, "W": 52, "M": 12, "Q": 4, "Y": 1}
_ALIAS = {"pct": "mom"}


def infer_frequency(dates: Any) -> str | None:
    """按日期序列中位间隔（天）推断频率：D/W/M/Q/Y

    <2 天 → D；<=8 → W；<=35 → M；<=95 → Q；否则 Y。
    序列不足 2 点或不可解析 → None。
    """
    if dates is None or len(dates) < 2:
        return None
    try:
        s = pd.Series(pd.DatetimeIndex(dates)).sort_values().reset_index(drop=True)
    except Exception:
        return None
    gaps = s.diff().dropna().dt.days
    if gaps.empty:
        return None
    med = float(gaps.median())
    if med < 2:
        return "D"
    if med <= 8:
        return "W"
    if med <= 35:
        return "M"
    if med <= 95:
        return "Q"
    return "Y"


def compute_transform(
    df: pd.DataFrame,
    transform: str = "level",
    freq: str | None = None,
) -> pd.DataFrame:
    """对 {date, value} 两列做派生，返回 {date, value}，value 为 float（缺失/无穷→NaN）

    入参 value 可为 TEXT 字符串；内部先 to_numeric。日期列解析 errors="coerce"，
    内部升序（兼容 get_indicator 的降序输入）。未知 transform 抛 ValueError。
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "value"])

    method = _ALIAS.get(transform, transform)
    if method not in TRANSFORMS:
        raise ValueError(f"未知 transform: {transform}（可用: level/yoy/mom/pct）")

    out = df[["date", "value"]].copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if out.empty:
        return pd.DataFrame(columns=["date", "value"])

    # 值转数值（TEXT 存储可能是字符串），inf → NaN
    vals = pd.to_numeric(out["value"], errors="coerce").astype(float)
    vals = vals.replace([np.inf, -np.inf], np.nan)

    if method == "level":
        out["value"] = vals
        return out

    # mom = 环比（上一期，恒 shift=1）；yoy = 同比（一年前，shift 依频率）
    if method == "mom":
        shift = 1
    else:  # yoy
        shift = _FREQ_SHIFTS.get(freq or infer_frequency(out["date"]) or "M", 12)
    pct = (vals / vals.shift(shift) - 1.0) * 100.0
    out["value"] = pct.replace([np.inf, -np.inf], np.nan)
    return out
