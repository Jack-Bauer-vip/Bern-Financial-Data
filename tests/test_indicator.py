"""指标归一层测试 — meta_indicator 映射、统一查询、候选收集（全离线）"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository


@pytest.fixture()
def repo():
    """临时库：FRED 风格 {date,value} 表 + akshare 风格 {日期,今值} 表"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    r.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_fred_x (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE macro_ak_x (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"日期" TEXT, "今值" TEXT, "前值" TEXT, created_at TEXT)'))
        # 插数据（SQLAlchemy 2.0 executemany 需 dict 列表）
        c.execute(
            text('INSERT INTO macro_fred_x (date, value) VALUES (:d, :v)'),
            [{"d": "2026-01-01", "v": "4.3"}, {"d": "2026-02-01", "v": "4.2"}])
        c.execute(
            text('INSERT INTO macro_ak_x ("日期", "今值") VALUES (:d, :v)'),
            [{"d": "2026-01-01", "v": "4.4"}, {"d": "2026-02-01", "v": "4.1"}])
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_set_and_get_indicator_fred(repo):
    """set_indicator 解析列映射 → get_indicator 返回首选表 {date,value}"""
    rec = repo.set_indicator("us.x", "macro_fred_x")
    assert rec is not None
    assert rec["preferred_table"] == "macro_fred_x"
    assert rec["date_column"] == "date"
    assert rec["value_column"] == "value"

    df = repo.get_indicator("us.x")
    assert list(df.columns) == ["date", "value"]
    # 倒序（最新在前）；值为 TEXT 列读出为字符串
    assert df.iloc[0]["date"] == "2026-02-01"
    assert df.iloc[0]["value"] == "4.2"


def test_set_indicator_switch_preferred(repo):
    """切换首选源到 akshare 表 → get_indicator 返回 akshare 数据且列为 date/value"""
    repo.set_indicator("us.x", "macro_fred_x")
    rec = repo.set_indicator("us.x", "macro_ak_x")
    assert rec["preferred_table"] == "macro_ak_x"
    assert rec["date_column"] == "日期"
    assert rec["value_column"] == "今值"

    df = repo.get_indicator("us.x")
    assert list(df.columns) == ["date", "value"]
    # akshare 数据（TEXT 列）
    assert df.iloc[0]["value"] == "4.1"


def test_get_indicator_no_mapping(repo):
    """无映射 → 空 DataFrame（仍两列）"""
    df = repo.get_indicator("us.nonexistent")
    assert df.empty
    assert list(df.columns) == ["date", "value"]


def test_get_indicator_date_range(repo):
    """支持日期区间过滤"""
    repo.set_indicator("us.x", "macro_fred_x")
    df = repo.get_indicator("us.x", start_date="2026-02-01", end_date="2026-02-28")
    assert len(df) == 1
    assert df.iloc[0]["date"] == "2026-02-01"


def test_list_indicators_roundtrip(repo):
    """list_indicators 往返"""
    assert repo.list_indicators() == []
    repo.set_indicator("us.x", "macro_fred_x")
    items = repo.list_indicators()
    assert len(items) == 1
    assert items[0]["indicator_key"] == "us.x"
    assert items[0]["preferred_table"] == "macro_fred_x"


def test_set_indicator_invalid_table(repo):
    """表不存在 → None"""
    assert repo.set_indicator("us.x", "no_such_table") is None


def test_meta_indicator_excluded_from_data_tables(repo):
    """meta_indicator 是元数据表，collect_tables 不应暴露为数据表"""
    from src.importer.matcher import collect_tables
    repo.set_indicator("us.x", "macro_fred_x")
    tables = collect_tables(repo)
    names = {t.table_name for t in tables}
    assert "macro_fred_x" in names
    assert "macro_ak_x" in names
    assert "meta_indicator" not in names


def test_indicator_candidates(repo):
    """indicator_candidates 从目录收集同一指标的所有源"""
    cands = repo.indicator_candidates("us.unemployment")
    tables = {c["table_name"] for c in cands}
    assert tables == {"macro_usa_unemployment_rate", "macro_fred_unemployment"}
    # 无 indicator 键的指标 → 空
    assert repo.indicator_candidates("us.ism") == []


# ---------------------------------------------------------------------------
# 集成：get_indicator_map / auto_adopt / /macro/cpi 走统一接口
# ---------------------------------------------------------------------------


def test_get_indicator_map_roundtrip(repo):
    """get_indicator_map 往返"""
    assert repo.get_indicator_map("us.x") is None
    repo.set_indicator("us.x", "macro_fred_x")
    m = repo.get_indicator_map("us.x")
    assert m["preferred_table"] == "macro_fred_x"
    assert m["date_column"] == "date"
    assert m["value_column"] == "value"


def test_auto_adopt_first_source(repo):
    """auto_adopt：未设获信源 + 数值列明确（今值）→ 自动采用"""
    rec = repo.auto_adopt_indicator("us.x", "macro_ak_x")
    assert rec is not None
    assert rec["preferred_table"] == "macro_ak_x"
    assert rec["value_column"] == "今值"
    assert repo.get_indicator_map("us.x")["preferred_table"] == "macro_ak_x"


def test_auto_adopt_not_override_existing(repo):
    """auto_adopt 不覆盖已手动的获信源"""
    repo.set_indicator("us.x", "macro_fred_x")
    assert repo.auto_adopt_indicator("us.x", "macro_ak_x") is None
    assert repo.get_indicator_map("us.x")["preferred_table"] == "macro_fred_x"


def test_auto_adopt_ohlc_table_skipped(repo):
    """auto_adopt 对 OHLC 表（无 今值/现值/value 列）不自动采用"""
    with repo.engine.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_ohlc (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "open" TEXT, "close" TEXT, created_at TEXT)'))
        c.execute(text(
            "INSERT INTO macro_ohlc (date, open, close) VALUES "
            "('2026-01-01','1.0','1.1')"))
    assert repo.auto_adopt_indicator("us.ohlc", "macro_ohlc") is None
    assert repo.get_indicator_map("us.ohlc") is None


def test_macro_cpi_uses_trusted_indicator(repo):
    """/macro/cpi：us.cpi 获信源设为 FRED 表 → 端点返回 FRED 数据"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.api import routes

    # 建 FRED 风格 CPI 表并设为 us.cpi 获信源
    with repo.engine.begin() as c:
        c.execute(text(
            'CREATE TABLE macro_fred_cpi (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "value" TEXT, created_at TEXT)'))
        c.execute(text(
            "INSERT INTO macro_fred_cpi (date, value) VALUES "
            "('2026-01-01','310.0'),('2026-02-01','312.0')"))
    repo.set_indicator("us.cpi", "macro_fred_cpi")

    routes.set_repo(repo)
    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app)

    resp = client.get("/macro/cpi")
    assert resp.status_code == 200
    data = resp.json()["data"]
    # 获信源 FRED 数据应出现在结果里
    assert any(
        isinstance(r, dict) and r.get("date") == "2026-01-01"
        and str(r.get("value")) == "310.0"
        for r in data
    )
