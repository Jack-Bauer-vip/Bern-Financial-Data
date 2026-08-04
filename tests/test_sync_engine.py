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
