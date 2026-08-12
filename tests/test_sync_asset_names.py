# -*- coding: utf-8 -*-
"""资产名称同步脚本测试(scripts_gen/sync_asset_names.py, mock akshare)

验证: 建表 + 只写「在库 code」+ 幂等(二次 0 新增) + 接口异常单类型跳过不崩溃。
"""

import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.core.dynamic_schema import DynamicSchemaManager
from src.db.repository import DataRepository


@pytest.fixture()
def asset_env(tmp_path):
    """临时库: fund_etf_daily 两只在库 code + index_daily 一个 symbol"""
    db = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    schema = DynamicSchemaManager(repo)

    schema.ensure_table_exists("fund_etf_daily", ["date", "close", "code"])
    df = pd.DataFrame({
        "date": ["2026-08-01", "2026-08-01"],
        "close": ["10.0", "20.0"],
        "code": ["159915", "510300"],
    })
    repo.bulk_upsert("fund_etf_daily", df, unique_columns=["date", "code"])

    schema.ensure_table_exists("index_daily", ["date", "close", "symbol"])
    idx = pd.DataFrame({
        "date": ["2026-08-01"], "close": ["3200.0"], "symbol": ["sh000001"],
    })
    repo.bulk_upsert("index_daily", idx, unique_columns=["date", "symbol"])

    # 与脚本 main() 相同的建表/索引步骤(meta_asset_info 由 DynamicSchemaManager 建)
    schema.ensure_table_exists("meta_asset_info", ["code", "name", "asset_type"])
    repo.ensure_unique_index("meta_asset_info", ["asset_type", "code"], dedupe=True)

    yield repo, schema
    eng.dispose()
    import os
    if os.path.exists(db):
        os.remove(db)


def _import_script():
    import scripts_gen.sync_asset_names as san
    return san


def test_sync_fund_writes_only_in_db_codes(asset_env, monkeypatch):
    """meta_asset_info 只落「在库 code」; 表外 code 与名称一律不写"""
    repo, _ = asset_env
    san = _import_script()
    df = pd.DataFrame({
        "基金代码": ["159915", "510300", "999999"],
        "基金简称": ["创业板ETF易方达", "沪深300ETF", "表外基金"],
    })
    monkeypatch.setattr(san, "ak_fund_names", lambda: df)

    n = san.sync_asset_type(repo, "fund")
    assert n == 2  # 只写 2 只在库 code

    names = repo.get_asset_names("fund")
    assert names == {"159915": "创业板ETF易方达", "510300": "沪深300ETF"}
    assert "999999" not in names


def test_sync_fund_idempotent(asset_env, monkeypatch):
    """二次运行内容一致 → 0 新增(幂等)"""
    repo, _ = asset_env
    san = _import_script()
    df = pd.DataFrame({"基金代码": ["159915"], "基金简称": ["创业板ETF易方达"]})
    monkeypatch.setattr(san, "ak_fund_names", lambda: df)

    assert san.sync_asset_type(repo, "fund") == 1
    assert san.sync_asset_type(repo, "fund") == 0


def test_sync_index_normalizes_codes(asset_env, monkeypatch):
    """指数裸代码归一为 sh/sz 前缀后, 只写与 index_daily.symbol 匹配的行"""
    repo, _ = asset_env
    san = _import_script()
    df = pd.DataFrame({
        "index_code": ["000001", "399001", "000300"],
        "display_name": ["上证指数", "深证成指", "沪深300"],
    })
    monkeypatch.setattr(san, "ak_index_names", lambda: df)

    n = san.sync_asset_type(repo, "index")
    assert n == 1  # 只有 sh000001 在库; sh000300 不在库被滤掉
    names = repo.get_asset_names("index")
    assert names.get("sh000001") == "上证指数"
    assert "sz399001" not in names


def test_fetch_with_retry_gives_up_after_attempts(monkeypatch):
    """接口连续失败 → 3 次重试后返回 None(不抛出)"""
    san = _import_script()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ConnectionError("network")

    monkeypatch.setattr(san.time, "sleep", lambda s: None)
    assert san._fetch_with_retry(boom, attempts=3) is None
    assert calls["n"] == 3


def test_sync_asset_failure_skips_without_crash(asset_env, monkeypatch):
    """akshare 接口异常 → 单类型跳过(返回 0), 不崩溃、不写任何行"""
    repo, _ = asset_env
    san = _import_script()

    def boom():
        raise ConnectionError("network down")

    monkeypatch.setattr(san, "ak_fund_names", boom)
    monkeypatch.setattr(san.time, "sleep", lambda s: None)

    n = san.sync_asset_type(repo, "fund")
    assert n == 0
    assert repo.get_asset_names("fund") == {}
