"""Repository 唯一索引 / 去重 / bulk_upsert 测试

用临时 SQLite 库隔离，不碰生产数据。
运行: python -m pytest tests/test_repository_dedupe.py -v
"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository

DDL_CPI = (
    'CREATE TABLE macro_usa_cpi_yoy ('
    'id INTEGER PRIMARY KEY AUTOINCREMENT,'
    '"时间" TEXT, "日期" TEXT, "数值" TEXT, "前值" TEXT, created_at TEXT)'
)


@pytest.fixture()
def repo():
    """临时库 + 一张 CPI 表（真实日期列是「时间」）"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    with eng.begin() as c:
        c.execute(text(DDL_CPI))
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_resolve_date_columns(repo):
    """逻辑日期列 date 应解析到真实列「时间」"""
    assert repo.resolve_date_columns("macro_usa_cpi_yoy", ["date"]) == ["时间"]
    # symbol 不存在应被丢弃
    assert repo.resolve_date_columns("macro_usa_cpi_yoy", ["symbol", "date"]) == ["时间"]


def test_duplicate_count_and_dedupe(repo):
    with repo.engine.begin() as c:
        c.execute(text(
            "INSERT INTO macro_usa_cpi_yoy (\"时间\",\"日期\",\"数值\") VALUES "
            "('2026-07-01','2026-08-12','3.5'), "
            "('2026-07-01','2026-08-12','3.6'), "
            "('2026-06-01','2026-07-14','3.2')"))
    assert repo.get_duplicate_count("macro_usa_cpi_yoy", ["date"]) == 1
    deleted = repo.dedupe_rows("macro_usa_cpi_yoy", ["date"])
    assert deleted == 1
    assert repo.get_duplicate_count("macro_usa_cpi_yoy", ["date"]) == 0


def test_ensure_unique_index_with_existing_dupes(repo):
    """已有重复行时，ensure_unique_index 应先去重再建索引"""
    with repo.engine.begin() as c:
        c.execute(text(
            "INSERT INTO macro_usa_cpi_yoy (\"时间\",\"数值\") VALUES "
            "('2026-07-01','3.5'), ('2026-07-01','3.6')"))
    assert repo.ensure_unique_index("macro_usa_cpi_yoy", ["date"]) is True
    assert repo.index_exists("macro_usa_cpi_yoy", ["date"]) is True
    assert repo.get_duplicate_count("macro_usa_cpi_yoy", ["date"]) == 0


def test_bulk_upsert_updates_not_duplicates(repo):
    """bulk_upsert 按唯一键更新已存在行，不产生重复"""
    with repo.engine.begin() as c:
        c.execute(text(
            "INSERT INTO macro_usa_cpi_yoy (\"时间\",\"数值\") VALUES "
            "('2026-07-01','3.5'), ('2026-06-01','3.2')"))
    df = pd.DataFrame([{"时间": "2026-07-01", "数值": "3.8"}])
    repo.bulk_upsert("macro_usa_cpi_yoy", df, unique_columns=["date"])
    with repo.engine.connect() as c:
        n = c.execute(text("SELECT COUNT(*) FROM macro_usa_cpi_yoy")).scalar()
        v = c.execute(
            text("SELECT \"数值\" FROM macro_usa_cpi_yoy WHERE \"时间\"='2026-07-01'")
        ).scalar()
    assert n == 2          # 不新增重复
    assert v == "3.8"      # 更新生效


def test_bulk_upsert_no_unique_cols_still_inserts(repo):
    """不传唯一键时退化为纯插入（兼容旧行为）"""
    df = pd.DataFrame([{"时间": "2026-07-01", "数值": "3.5"},
                       {"时间": "2026-07-01", "数值": "3.6"}])
    added = repo.bulk_upsert("macro_usa_cpi_yoy", df)
    assert added == 2


def test_bulk_upsert_returns_true_new_rows(repo):
    """返回值为真正新增行数，而非处理行数（静默停更识别依赖）"""
    # 首次写入 2 行 → 返回 2
    df1 = pd.DataFrame([{"时间": "2026-06-01", "数值": "3.2"},
                        {"时间": "2026-07-01", "数值": "3.5"}])
    assert repo.bulk_upsert(
        "macro_usa_cpi_yoy", df1, unique_columns=["date"]) == 2
    # 重同步同日期（含同值 no-op 更新）→ 0 新增，即使行被「处理」
    df2 = pd.DataFrame([{"时间": "2026-07-01", "数值": "3.5"}])
    assert repo.bulk_upsert(
        "macro_usa_cpi_yoy", df2, unique_columns=["date"]) == 0
    # 更新已存在行（新值）+ 新增一行 → 只计新增 1
    df3 = pd.DataFrame([{"时间": "2026-07-01", "数值": "3.8"},
                        {"时间": "2026-08-01", "数值": "3.6"}])
    assert repo.bulk_upsert(
        "macro_usa_cpi_yoy", df3, unique_columns=["date"]) == 1
    # 更新已存在行（新值）+ 新增一行 → 只计新增 1
    with repo.engine.connect() as c:
        n = c.execute(text(
            "SELECT COUNT(*) FROM macro_usa_cpi_yoy")).scalar()
        v = c.execute(text(
            "SELECT \"数值\" FROM macro_usa_cpi_yoy WHERE \"时间\"='2026-07-01'")
        ).scalar()
    assert n == 3
    assert v == "3.8"


def test_bulk_upsert_batch_internal_dupe_counts_once(repo):
    """同批内重复唯一键只算一次新增（SQLite 首插后续变冲突更新）"""
    df = pd.DataFrame([{"时间": "2026-07-01", "数值": "3.5"},
                       {"时间": "2026-07-01", "数值": "3.6"}])
    assert repo.bulk_upsert(
        "macro_usa_cpi_yoy", df, unique_columns=["date"]) == 1
