# -*- coding: utf-8 -*-
"""行情复权 — 复权因子派生（纯 pandas，零 GUI/DB/API 依赖）

对行情查询结果做前/后复权派生，行情表存不复权原始价（ODS 原则永不改），
查询端应用复权因子表 asset_adj_factor：

- qfq 前复权 : price × factor / latest_factor(code)
  其中 latest_factor = 该 code 因子表全序列最大日期的 factor。价格随时间回落到最新，
  历史价格与最新价可比（技术分析默认口径）。
- hfq 后复权 : price × factor
  价格随时间复利放大，反映真实累计收益（回测口径）。

volume/amount 不处理：成交量是股数/手数计数，复权口径不改成交量。

因子语义：tushare 官方因子为绝对累计复权因子；akshare 回退路径存 hfq/raw 相对因子。
两条公式对二者都成立（满足 qfq(t)=raw(t)·F(t)/F(latest)、hfq(t)=raw(t)·F(t)/F(归一化点)
的相对结构），故共用同一 apply_adjustment。
"""

from typing import Any

import numpy as np
import pandas as pd

ADJ_TYPES = {"qfq", "hfq"}

# 需要乘因子的价格列（存在才处理）
_PRICE_COLS = ("open", "high", "low", "close")


def apply_adjustment(
    df: pd.DataFrame,
    adj_type: str,
    factor_df: pd.DataFrame,
    code_col: str = "code",
) -> pd.DataFrame:
    """对查询结果 OHLC 做复权派生，返回新 DataFrame（原 df 不修改）

    df         : 行情查询结果，至少含 date + OHLC；可能含 code/symbol/volume/amount 等
    adj_type   : "qfq" | "hfq"，非法抛 ValueError
    factor_df  : 因子表，列 {code, date, factor}（多 code）或 {date, factor}（单 code）；
                 必须是该 code 的【全量】因子序列（qfq 的 latest 需全序列）
    code_col   : df 中的代码列名（与 factor_df 的 code 列对齐）

    返回：仅 OHLC 列被乘，其余列（volume/amount/code 等）原样保留。
    因子缺失（早于首个因子日 / 晚于末个因子日）的行丢弃。
    """
    if df is None or df.empty:
        return df.copy() if df is not None else df

    method = str(adj_type).lower()
    if method not in ADJ_TYPES:
        raise ValueError(f"未知复权类型: {adj_type}（可用: qfq/hfq）")

    if factor_df is None or factor_df.empty:
        raise ValueError("复权因子为空，无法派生")

    out = df.copy()
    out["_dt"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["_dt"])
    if out.empty:
        out = out.drop(columns=["_dt"])
        return out

    # 因子表预处理
    f = factor_df.copy()
    f["_dt"] = pd.to_datetime(f["date"], errors="coerce")
    f["factor"] = pd.to_numeric(f["factor"], errors="coerce")
    f = f.dropna(subset=["_dt", "factor"])

    has_code = code_col in out.columns and code_col in f.columns

    if has_code:
        # 多 code：每组取全序列最新因子作为 qfq 归一化点
        f = f.assign(
            _latest=f.sort_values("_dt").groupby(code_col)["factor"].transform("last")
        )
        merged = out.merge(
            f[[code_col, "_dt", "factor", "_latest"]],
            on=[code_col, "_dt"],
            how="left",
        )
        # 组内按日期前向填充（因子在除权日之间为分段常数）；仍缺（早于首个
        # 因子日 / 该 code 无因子）的行 dropna 丢弃
        merged = merged.sort_values([code_col, "_dt"])
        merged[["factor", "_latest"]] = merged.groupby(
            code_col)[["factor", "_latest"]].ffill()
        merged = merged.dropna(subset=["factor"])
        # qfq 需按 latest 归一化，hfq 直乘因子
        if method == "qfq":
            merged["_mult"] = merged["factor"] / merged["_latest"]
        else:
            merged["_mult"] = merged["factor"]
    else:
        # 单 code（或 df 无 code 列）：因子按日期左连 + ffill
        merged = out.merge(f[["_dt", "factor"]], on="_dt", how="left")
        merged = merged.sort_values("_dt")
        merged["factor"] = merged["factor"].ffill()
        merged = merged.dropna(subset=["factor"])
        if method == "qfq":
            latest_val = float(f.sort_values("_dt")["factor"].iloc[-1])
            merged["_mult"] = merged["factor"] / latest_val
        else:
            merged["_mult"] = merged["factor"]

    for col in _PRICE_COLS:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce") * merged["_mult"]

    drop_cols = ["_dt", "factor", "_mult"] + (["_latest"] if has_code else [])
    merged = merged.drop(columns=[c for c in drop_cols if c in merged.columns])
    # 还原原始列顺序（out 含临时 _dt，用 df.columns 还原）
    return merged[df.columns.tolist()]


def derive_factor_from_prices(
    adjusted: pd.DataFrame,
    raw: pd.DataFrame,
) -> pd.DataFrame:
    """从「复权价序列 ÷ 不复权序列」反推相对复权因子，返回 {date, factor}

    adjusted : akshare 复权序列，已规范列名，含 date + close（前/后复权）
    raw      : akshare 不复权序列，含 date + close

    必须传 hfq（后复权）序列：存储的 factor = F(t)/F(first)，则
      qfq 查询 = raw × factor/latest = raw × F(t)/F(latest)   ✓ 精确复现 akshare qfq
      hfq 查询 = raw × factor        = raw × F(t)/F(first)    ✓ 精确复现 akshare hfq
    若误传 qfq 序列，hfq 查询会退化为 qfq 序列（常数倍偏差），故必须用 hfq。
    """
    if adjusted is None or raw is None:
        return pd.DataFrame(columns=["date", "factor"])
    if adjusted.empty or raw.empty:
        return pd.DataFrame(columns=["date", "factor"])

    a = adjusted[["date", "close"]].copy()
    r = raw[["date", "close"]].copy()
    a["_dt"] = pd.to_datetime(a["date"], errors="coerce")
    r["_dt"] = pd.to_datetime(r["date"], errors="coerce")
    a["_close"] = pd.to_numeric(a["close"], errors="coerce")
    r["_close"] = pd.to_numeric(r["close"], errors="coerce")

    merged = a[["_dt", "_close"]].merge(r[["_dt", "_close"]], on="_dt", suffixes=("_adj", "_raw"))
    merged = merged.dropna(subset=["_close_adj", "_close_raw"])
    # 分母为 0 保护（异常行情/停牌空值）
    merged = merged[merged["_close_raw"] != 0]
    if merged.empty:
        return pd.DataFrame(columns=["date", "factor"])

    merged["factor"] = merged["_close_adj"] / merged["_close_raw"]
    # 除权日之间因子为分段常数：按日期排序后前向填充（先给整序列填充，
    # 再按 date 列输出，日期格式保持与入库规范一致）
    merged = merged.sort_values("_dt")
    out = pd.DataFrame({
        "date": merged["_dt"].dt.strftime("%Y-%m-%d"),
        "factor": merged["factor"],
    })
    return out.dropna(subset=["factor"])


def is_tushare_permission_error(exc: Exception) -> bool:
    """tushare 报错是否为「积分/权限不足」——据此决定是否切换到 akshare 回退路径"""
    msg = str(exc).lower()
    return any(k in msg for k in ("积分", "权限", "permission", "无权限", "无此接口"))
