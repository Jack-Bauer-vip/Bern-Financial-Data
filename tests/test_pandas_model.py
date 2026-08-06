# -*- coding: utf-8 -*-
"""PandasModel 绘制路径回归测试 — 锁定 7fd6a87 卡顿修复

data() 是绘制热点：旧实现每个单元格都 iloc[:, col].dtype（复制整列 O(行数)）
+ iloc[row, col]，5000 行大表滚动/重绘每格 O(N)。新实现 setDataFrame 时一次性
tolist() 缓存列值，data() 只做纯列表索引 O(1)。
本测试用 1000+ 行混合类型 DataFrame 循环调 data() 数千次，断言：
不抛 IndexError / 数值格式化正确 / NaN/None 显示空串 / 对齐角色正确。
"""

import numpy as np
import pandas as pd
from PySide6.QtCore import Qt

from src.gui.table_view import PandasModel, sort_df_by_date_desc


def _build_df(n: int = 1500) -> pd.DataFrame:
    """构造 n 行混合类型 DataFrame（float / NaN / None / int / 字符串日期）"""
    rng = np.random.default_rng(42)
    dates = pd.date_range("2020-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates,
        "open": rng.uniform(1, 100, n).round(4),
        "close": np.where(
            rng.random(n) < 0.1, np.nan, rng.uniform(1, 100, n).round(4)),
        "volume": rng.integers(0, 1_000_000, n),
        "name": [f"基金{i % 50}" for i in range(n)],
        "big": np.full(n, 123456.789),
        "nulls": [None if i % 7 == 0 else rng.uniform(1, 10) for i in range(n)],
    })


def test_data_thousands_of_calls_no_error():
    """1000+ 行混合表，data() 数千次调用不抛异常、不越界"""
    model = PandasModel()
    df = _build_df(1500)
    model.setDataFrame(df)

    rows, cols = model.rowCount(), model.columnCount()
    assert rows == 1500 and cols == 7

    # 三轮全表扫描 × 双角色 ≈ 6.3 万次 data() 调用
    for _ in range(3):
        for r in range(rows):
            for c in range(cols):
                idx = model.index(r, c)
                assert idx.isValid()
                model.data(idx, Qt.ItemDataRole.DisplayRole)
                model.data(idx, Qt.ItemDataRole.TextAlignmentRole)


def test_data_formatting_rules():
    """数值格式化：|v|<10000 四位小数、>=10000 两位小数；NaN/None 显示空串"""
    model = PandasModel()
    df = _build_df(1500)
    model.setDataFrame(df)

    r = 10
    open_val = float(df.iloc[r]["open"])          # < 10000 → 4 位小数
    assert model.data(model.index(r, 1),
                      Qt.ItemDataRole.DisplayRole) == f"{open_val:.4f}"
    assert model.data(model.index(r, 5),
                      Qt.ItemDataRole.DisplayRole) == "123456.79"   # >= 10000 → 2 位

    # NaN → 空串（找第一个 NaN 所在行）
    nan_row = int(df["close"].isna().idxmax())
    assert model.data(model.index(nan_row, 2),
                      Qt.ItemDataRole.DisplayRole) == ""

    # None → 空串（nulls 列第 0 行是 None）
    assert model.data(model.index(0, 6),
                      Qt.ItemDataRole.DisplayRole) == ""

    # int 列 → 原样字符串
    vol = int(df.iloc[r]["volume"])
    assert model.data(model.index(r, 3),
                      Qt.ItemDataRole.DisplayRole) == str(vol)


def test_text_alignment_role():
    """数值列右对齐 + 垂直居中；字符串列居中"""
    model = PandasModel()
    model.setDataFrame(_build_df(100))
    right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    center = int(Qt.AlignmentFlag.AlignCenter)

    assert model.data(model.index(0, 1),
                      Qt.ItemDataRole.TextAlignmentRole) == right   # open
    assert model.data(model.index(0, 5),
                      Qt.ItemDataRole.TextAlignmentRole) == right   # big
    assert model.data(model.index(0, 4),
                      Qt.ItemDataRole.TextAlignmentRole) == center  # name


def test_dataframe_roundtrip():
    """getDataFrame 返回副本，不共享内部引用"""
    model = PandasModel()
    df = _build_df(10)
    model.setDataFrame(df)
    out = model.getDataFrame()
    out.iloc[0, 0] = "改"
    assert model.data(model.index(0, 0),
                      Qt.ItemDataRole.DisplayRole) != "改"


def test_sort_df_by_date_desc():
    """按日期倒序（最新在前）；无日期列原样返回；空表/异常不崩"""
    df = pd.DataFrame({
        "date": ["2026-01-03", "2026-01-01", "2026-01-02"],
        "v": [3, 1, 2],
    })
    out = sort_df_by_date_desc(df)
    assert out["date"].tolist() == ["2026-01-03", "2026-01-02", "2026-01-01"]
    assert out["v"].tolist() == [3, 2, 1]

    # 无日期列 → 原样返回同一对象
    df2 = pd.DataFrame({"x": [1, 2]})
    assert sort_df_by_date_desc(df2) is df2

    # 空表 / 非法日期不崩
    assert sort_df_by_date_desc(pd.DataFrame()).empty
    bad = pd.DataFrame({"date": ["x", "y"], "v": [1, 2]})
    assert sort_df_by_date_desc(bad) is bad
