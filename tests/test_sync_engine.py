"""同步引擎数据清洗测试 — 中文年月日归一化 + NaT 防御

回归：akshare 宏观函数日期列是 "2008年01月"（年+月、无日），pd.to_datetime
解析不了会整列变 NaT，写库时 SQLite 报
「Error binding parameter ... type 'NaTType' is not supported」。
"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.core.sync_engine import SyncEngine
from src.db.repository import DataRepository


# ---------------------------------------------------------------------------
# _clean_data 中文年月日归一化
# ---------------------------------------------------------------------------


def test_clean_data_normalizes_chinese_year_month():
    """2008年01月 / 2008年1月 → date(2008,1,1)"""
    df = pd.DataFrame({
        "时间": ["2008年01月", "2008年2月"],
        "现值": [5.21, 5.2],
    })
    out = SyncEngine._clean_data(df)
    assert out["时间"].tolist() == [pd.Timestamp("2008-01-01").date(),
                                    pd.Timestamp("2008-02-01").date()]
    assert len(out) == 2


def test_clean_data_normalizes_chinese_full_date():
    """2008年01月15日 → date(2008,1,15)"""
    df = pd.DataFrame({"时间": ["2008年01月15日"], "现值": [5.21]})
    out = SyncEngine._clean_data(df)
    assert out["时间"].tolist() == [pd.Timestamp("2008-01-15").date()]


def test_clean_data_iso_untouched():
    """ISO 日期不受影响"""
    df = pd.DataFrame({"时间": ["2008-01-01", "2008-02-01"], "现值": [1, 2]})
    out = SyncEngine._clean_data(df)
    assert out["时间"].tolist() == [pd.Timestamp("2008-01-01").date(),
                                    pd.Timestamp("2008-02-01").date()]


@pytest.mark.parametrize("raw,expected", [
    ("2009第4季度", "2009-12-01"),
    ("2009年第4季度", "2009-12-01"),
    ("2009年第四季度", "2009-12-01"),
    ("2009Q4", "2009-12-01"),
    ("2009年1季度", "2009-03-01"),
    ("2009第1季度", "2009-03-01"),
])
def test_clean_data_normalizes_quarter(raw, expected):
    """季度格式 → 季度末月（3/6/9/12）"""
    df = pd.DataFrame({"时间": [raw], "现值": [100.0]})
    out = SyncEngine._clean_data(df)
    assert out["时间"].tolist() == [pd.Timestamp(expected).date()]
    assert len(out) == 1


def test_clean_data_quarter_non_iso_string():
    """_normalize_cn_date_str 对季度串产出可解析的 ISO 前缀"""
    assert SyncEngine._normalize_cn_date_str("2009第4季度") == "2009-12"
    assert SyncEngine._normalize_cn_date_str("2009年1季度") == "2009-03"
    assert SyncEngine._normalize_cn_date_str("2009Q2") == "2009-06"
    # 无季度/中文日期字样 → 原样
    assert SyncEngine._normalize_cn_date_str("plain-text") == "plain-text"
    assert SyncEngine._normalize_cn_date_str(12345) == 12345


def test_extract_max_date_chinese_month():
    """_extract_max_date 识别中文「月份」列（'2026年06月份'→2026-06-01）"""
    df = pd.DataFrame({
        "月份": ["2026年05月份", "2026年06月份"],
        "全国-同比增长": [0.3, 1.0],
    })
    assert SyncEngine._extract_max_date(df) == pd.Timestamp("2026-06-01").date()


def test_extract_max_date_quarter():
    """_extract_max_date 识别中文「季度」列（'2026年第2季度'→2026-06-01）"""
    df = pd.DataFrame({
        "季度": ["2025年第4季度", "2026年第2季度"],
        "国内生产总值-同比增长": [5.0, 4.8],
    })
    assert SyncEngine._extract_max_date(df) == pd.Timestamp("2026-06-01").date()


def test_extract_max_date_iso_fallback():
    """无中文日期列时按 ISO 列解析（回归保护）"""
    df = pd.DataFrame({"date": ["2026-01-01", "2026-02-01"], "value": [1, 2]})
    assert SyncEngine._extract_max_date(df) == pd.Timestamp("2026-02-01").date()


def test_extract_max_date_no_parseable_returns_none():
    """无任何可解析日期列 → None（不误写 last_sync_date=today）"""
    df = pd.DataFrame({"value": [1, 2, 3]})
    assert SyncEngine._extract_max_date(df) is None


def test_clean_data_drops_unparseable_rows():
    """无法解析的日期行被丢弃而非保留 NaT"""
    df = pd.DataFrame({
        "时间": ["2008年01月", "???", None],
        "现值": [5.21, 9.9, 8.8],
    })
    out = SyncEngine._clean_data(df)
    # 只剩可解析的一行
    assert len(out) == 1
    assert out["时间"].tolist() == [pd.Timestamp("2008-01-01").date()]


# ---------------------------------------------------------------------------
# bulk_upsert 对 NaT 防御
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo():
    """临时库，含一张 {date, value} 表"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_test (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_bulk_upsert_handles_nat_in_value_column(repo):
    """数值列含 pd.NaT 不再崩库（NaT → NULL 写入）"""
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"],
        "value": [pd.NaT, 4.2],   # 第一行 value 是 NaT
    })
    n = repo.bulk_upsert("macro_test", df, unique_columns=["date"])
    assert n == 2


def test_bulk_upsert_handles_nat_in_date_column(repo):
    """日期列含 pd.NaT 不再崩库（NaT → NULL 写入）"""
    df = pd.DataFrame({
        "date": [pd.NaT, "2026-01-02"],
        "value": [1.0, 2.0],
    })
    n = repo.bulk_upsert("macro_test", df, unique_columns=["date"])
    assert n == 2


def test_bulk_upsert_handles_float_nan(repo):
    """float 列含 NaN 不再崩库（NaN → NULL 写入）

    pandas 3.x 下 float64 列 .where(None) 会把 None 强转回 nan，
    需先 astype(object)（回归：旧实现此用例红）。
    """
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"],
        "value": [float("nan"), 4.2],
    })
    n = repo.bulk_upsert("macro_test", df, unique_columns=["date"])
    assert n == 2
    rows = repo.query("macro_test").to_dict(orient="records")
    # NULL 读回为 NaN；若误存成字面量 "nan" 则 pd.isna 为 False，可区分
    assert pd.isna(rows[0]["value"])


def test_bulk_upsert_handles_string_pdna(repo):
    """pandas 3.x str dtype 列含 pd.NA → NULL 写入（NAType 不崩库）"""
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"],
        "value": pd.Series([pd.NA, "4.2"], dtype="string"),
    })
    n = repo.bulk_upsert("macro_test", df, unique_columns=["date"])
    assert n == 2
    rows = repo.query("macro_test").to_dict(orient="records")
    assert pd.isna(rows[0]["value"])


def test_bulk_upsert_object_nat(repo):
    """object 列含 pd.NaT / np.nan → NULL 写入"""
    import numpy as np
    df = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"],
        "value": [pd.NaT, np.nan],
    })
    n = repo.bulk_upsert("macro_test", df, unique_columns=["date"])
    assert n == 2


def test_ensure_index_creates_non_unique(repo):
    """ensure_index 创建普通非唯一索引（幂等）"""
    from sqlalchemy import text
    assert repo.ensure_index("macro_test", ["date"]) is True
    assert repo.ensure_index("macro_test", ["date"]) is True  # 幂等
    with repo.engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' "
            "AND name='idx_macro_test_date'"
        )).scalar()
    assert n == 1


def test_ensure_index_missing_column_false(repo):
    """表中无该列 → 返回 False（不建索引）"""
    assert repo.ensure_index("macro_test", ["symbol"]) is False


# ---------------------------------------------------------------------------
# 中文「月份/季度」列日期区间过滤（回归：2026-08-07 查询 bug）
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_cn():
    """临时库，含一张中文「月份」列宏观表 + ISO 列 FRED 风格表"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_cn (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"月份" TEXT, "全国-同比增长" TEXT, created_at TEXT)'))
        c.execute(text(
            'INSERT INTO macro_cn ("月份", "全国-同比增长") VALUES (:d, :v)'),
            [{"d": "2025年05月份", "v": "0.1"}, {"d": "2025年08月份", "v": "0.2"},
             {"d": "2025年12月份", "v": "0.3"}, {"d": "2026年06月份", "v": "1.0"}])
        c.execute(text(
            'CREATE TABLE macro_fred (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        c.execute(text(
            'INSERT INTO macro_fred (date, value) VALUES (:d, :v)'),
            [{"d": "2025-05-01", "v": "4"}, {"d": "2025-12-01", "v": "3"},
             {"d": "2026-06-01", "v": "2"}])
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_query_cn_month_date_range_iso(repo_cn):
    """中文「月份」列 + ISO 日期范围：保留 2025-12/2026-06，排除 2025-05

    回归：'2026年06月份' <= '2026-08-07' 的 SQL 字符串比较因 '年'(U+5E74) > '-'
    恒为 False，导致同年数据被 date_to 全部误杀（查询结果回退到旧年份）。
    """
    df = repo_cn.query("macro_cn", date_from="2025-08-07", date_to="2026-08-07")
    months = df["月份"].tolist()
    assert months == ["2025年12月份", "2026年06月份"]


def test_query_cn_month_date_range_ymd(repo_cn):
    """GUI 传入 YYYYMMDD 格式同样正确"""
    df = repo_cn.query("macro_cn", date_from="20250807", date_to="20260807")
    assert df["月份"].tolist() == ["2025年12月份", "2026年06月份"]


def test_query_iso_date_column_unchanged(repo_cn):
    """ISO 列（FRED 风格）过滤保持正确（走索引的原 SQL 路径）"""
    df = repo_cn.query("macro_fred", date_from="2025-08-07", date_to="2026-08-07")
    assert df["date"].tolist() == ["2025-12-01", "2026-06-01"]


def test_query_no_date_range_returns_all(repo_cn):
    """无日期参数 → 全量返回"""
    assert len(repo_cn.query("macro_cn")) == 4


def test_query_limit_orders_newest_first(repo_cn):
    """日期区间 + LIMIT → 返回区间内**最新**行（回归：日频大表被截断到旧日期）

    基金/指数日频表插入序=按 code，非日期序；原实现 LIMIT 先于排序截断，
    查询结果停在区间最旧的一小段（用户报告「基金查询回到 2025 年」）。
    """
    with repo_cn.engine.begin() as c:
        c.execute(text(
            'CREATE TABLE fund_x (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "code" TEXT, "close" TEXT, created_at TEXT)'))
        rows = []
        for code in ("000001", "000002", "000003"):
            for d in ("2025-08-07", "2025-12-01", "2026-06-01"):
                rows.append({"d": d, "c": code, "v": "1"})
        c.execute(text(
            'INSERT INTO fund_x (date, code, close) VALUES (:d, :c, :v)'), rows)
    df = repo_cn.query("fund_x", limit=4,
                       date_from="2025-08-07", date_to="2026-08-07")
    dates = df["date"].tolist()
    # 最新在前：3 个 code 的 2026-06-01 + 1 个 code 的 2025-12-01
    assert dates[0] == "2026-06-01"
    assert dates.count("2026-06-01") == 3
    assert dates[3] == "2025-12-01"
