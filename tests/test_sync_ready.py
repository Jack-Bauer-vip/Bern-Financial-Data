# -*- coding: utf-8 -*-
"""同步就绪标记测试 — write_sync_ready / load_sync_ready / compute_data_asof

就绪标记 data/sync_ready.json（含 data_asof / generated_at / tables）供 A/B
读前校验新鲜度；/api/v1/health 暴露 data_asof 字段（routes.py 消费本模块）。
"""

import os
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_ready import (
    CORE_READY_TABLES, compute_data_asof, load_sync_ready,
    sync_ready_path, write_sync_ready,
)
from src.db.repository import DataRepository


@pytest.fixture()
def repo():
    """临时库 + fund_etf_daily(今天) / index_daily(昨天) 各一行"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    repo = DataRepository(eng)
    repo.create_tables()
    schema = DynamicSchemaManager(repo)

    df = pd.DataFrame({
        "date": [date.today().isoformat()],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [10.0], "amount": [100.0], "code": ["159915"],
    })
    schema.ensure_columns("fund_etf_daily", list(df.columns))
    repo.bulk_upsert("fund_etf_daily", df, unique_columns=["code", "date"])

    idx = pd.DataFrame({
        "date": [(date.today() - timedelta(days=1)).isoformat()],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [10.0], "amount": [100.0], "symbol": ["sh000001"],
    })
    schema.ensure_columns("index_daily", list(idx.columns))
    repo.bulk_upsert("index_daily", idx, unique_columns=["symbol", "date"])

    yield repo
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_core_tables_defined():
    """核心就绪表 = A/B 消费的行情表"""
    assert "fund_etf_daily" in CORE_READY_TABLES
    assert "index_daily" in CORE_READY_TABLES


def test_compute_data_asof_cross_tables(repo):
    """跨核心表取最新交易日（fund 今天 > index 昨天 → 今天）"""
    assert compute_data_asof(repo) == date.today().isoformat()


def test_compute_data_asof_specific_tables(repo):
    """只算 index_daily → 昨天"""
    assert compute_data_asof(repo, ["index_daily"]) == (
        date.today() - timedelta(days=1)).isoformat()


def test_compute_data_asof_empty_repo():
    """无数据 → None（不抛异常）"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    repo = DataRepository(eng)
    repo.create_tables()
    assert compute_data_asof(repo) is None
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_write_and_load_roundtrip(repo):
    """写就绪标记 → 可读回，含 data_asof / generated_at / tables"""
    tmpdir = tempfile.mkdtemp()
    path = write_sync_ready(repo, ["fund_etf_daily"], data_dir=tmpdir)
    assert path is not None and path.exists()
    marker = load_sync_ready(data_dir=tmpdir)
    assert marker is not None
    assert marker["data_asof"] == date.today().isoformat()
    assert marker["tables"] == ["fund_etf_daily"]
    assert "generated_at" in marker


def test_write_default_tables_uses_core(repo):
    """不传 tables → 用 CORE_READY_TABLES（跨表取今天）"""
    tmpdir = tempfile.mkdtemp()
    path = write_sync_ready(repo, data_dir=tmpdir)
    assert path is not None
    marker = load_sync_ready(data_dir=tmpdir)
    assert marker["data_asof"] == date.today().isoformat()
    assert marker["tables"] == CORE_READY_TABLES


def test_load_missing_returns_none():
    """无标记文件 → None"""
    assert load_sync_ready(data_dir=tempfile.mkdtemp()) is None


def test_sync_ready_path_default_under_data():
    """默认路径在项目 data/ 下"""
    p = sync_ready_path()
    assert p.name == "sync_ready.json"
    assert p.parent.name == "data"


def test_write_missing_table_no_crash(repo):
    """涉及不存在的表 → data_asof=None，仍可写（不抛异常）"""
    tmpdir = tempfile.mkdtemp()
    path = write_sync_ready(repo, ["no_such_table"], data_dir=tmpdir)
    assert path is not None
    marker = load_sync_ready(data_dir=tmpdir)
    assert marker is not None
    assert marker["data_asof"] is None
