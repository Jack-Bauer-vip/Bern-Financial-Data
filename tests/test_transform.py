# -*- coding: utf-8 -*-
"""指标派生视图测试 — infer_frequency / compute_transform"""

import numpy as np
import pandas as pd
import pytest

from src.core.transform import compute_transform, infer_frequency


def _monthly(rows: int, base=100.0, step=1.0) -> pd.DataFrame:
    """构造 N 个月末序列，值从 base 起每次 +step"""
    dates = pd.date_range("2024-01-31", periods=rows, freq="ME")
    return pd.DataFrame({"date": dates, "value": [base + i * step for i in range(rows)]})


# ---------------------------------------------------------------------------
# infer_frequency
# ---------------------------------------------------------------------------


def test_infer_frequency_daily_weekly_monthly_quarterly_yearly():
    assert infer_frequency(pd.date_range("2026-01-01", periods=5, freq="D")) == "D"
    assert infer_frequency(pd.date_range("2026-01-01", periods=5, freq="W")) == "W"
    assert infer_frequency(pd.date_range("2026-01-31", periods=5, freq="ME")) == "M"
    # 季度末月（3/6/9/12）→ 中位间隔 ~92 天 → Q
    q = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31"]
    assert infer_frequency(pd.DatetimeIndex(q)) == "Q"
    assert infer_frequency(pd.date_range("2024-01-01", periods=3, freq="YE")) == "Y"


def test_infer_frequency_insufficient():
    assert infer_frequency(None) is None
    assert infer_frequency(pd.DatetimeIndex(["2026-01-01"])) is None
    assert infer_frequency([]) is None


# ---------------------------------------------------------------------------
# compute_transform
# ---------------------------------------------------------------------------


def test_level_passthrough_string_values():
    """TEXT 字符串值转数值透传"""
    df = pd.DataFrame({"date": ["2026-01-01", "2026-02-01"], "value": ["100", "101.5"]})
    out = compute_transform(df, "level")
    assert out["value"].tolist() == [100.0, 101.5]


def test_yoy_monthly():
    """月线同比：第 13 行 = (v13/v1 - 1)*100"""
    df = _monthly(13, base=100.0, step=10.0)  # v13 = 100 + 12*10 = 220
    out = compute_transform(df, "yoy")
    assert out.iloc[-1]["value"] == pytest.approx((220 / 100 - 1) * 100)
    # 前 12 行没有一年前前值 → NaN
    assert np.isnan(out.iloc[:12]["value"]).all()


def test_mom_monthly():
    """月线环比：第 2 行 = (v2/v1 - 1)*100"""
    df = _monthly(3, base=100.0, step=10.0)  # v1=100, v2=110, v3=120
    out = compute_transform(df, "mom")
    assert out.iloc[1]["value"] == pytest.approx(10.0)
    assert out.iloc[2]["value"] == pytest.approx(120 / 110 * 100 - 100, abs=1e-9)
    assert np.isnan(out.iloc[0]["value"])


def test_pct_is_alias_of_mom():
    df = _monthly(3, base=100.0, step=10.0)
    out_pct = compute_transform(df, "pct")
    out_mom = compute_transform(df, "mom")
    pd.testing.assert_frame_equal(out_pct, out_mom)


def test_quarterly_qoq():
    """季度数据 mom = QoQ：shift 1 即环比"""
    q = pd.DataFrame({
        "date": ["2024-03-31", "2024-06-30", "2024-09-30"],
        "value": [100.0, 105.0, 110.0],
    })
    out = compute_transform(q, "mom")
    assert out.iloc[1]["value"] == pytest.approx(5.0)


def test_division_by_zero_gives_nan():
    df = pd.DataFrame({"date": ["2026-01-01", "2026-02-01"], "value": [0.0, 5.0]})
    out = compute_transform(df, "mom")
    # 0 除 → inf → NaN
    assert np.isnan(out.iloc[1]["value"])


def test_inf_input_sanitized():
    df = pd.DataFrame({"date": ["2026-01-01", "2026-02-01"], "value": [np.inf, 5.0]})
    out = compute_transform(df, "level")
    assert np.isnan(out.iloc[0]["value"])


def test_descending_input_sorted_internally():
    """降序输入（get_indicator 返回）→ 结果与升序一致"""
    df = _monthly(3, base=100.0, step=10.0)
    desc = df.iloc[::-1].reset_index(drop=True)
    out = compute_transform(desc, "mom")
    assert out.iloc[1]["value"] == pytest.approx(10.0)


def test_unknown_transform_raises():
    df = _monthly(3)
    with pytest.raises(ValueError):
        compute_transform(df, "diff")


def test_empty_input():
    out = compute_transform(pd.DataFrame(), "yoy")
    assert list(out.columns) == ["date", "value"]
    assert len(out) == 0


def test_missing_values_mid_series():
    """中段缺失 → 仅受影响行 NaN，不崩"""
    df = pd.DataFrame({
        "date": ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"],
        "value": [100.0, np.nan, 110.0, 121.0],
    })
    out = compute_transform(df, "mom")
    assert np.isnan(out.iloc[1]["value"]) and np.isnan(out.iloc[2]["value"])
    assert out.iloc[3]["value"] == pytest.approx(121 / 110 * 100 - 100)
