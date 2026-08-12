# -*- coding: utf-8 -*-
"""/api/v1/search 代码搜索端点测试

搜索 = 表内实际 code(保证搜到的必有数据) + meta_asset_info 名称补全。
- 只返回表内 code: 名称表里放一个 daily 表不存在的 code, 不得出现在结果
- 无代码列的表 → 200 空(meta.code_column=null), 不 422
- 空 q → 前 limit 个 code(下拉初始化), limit 截断
- 非法表 → 404
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from src.api.server import FastAPIServer
from src.db.repository import DataRepository

FUND_CODES = {
    "159915": "创业板ETF易方达",
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "510050": "上证50ETF",
}
FAKE_CODE = "999999"  # 只在 meta_asset_info, 不在 daily 表


@pytest.fixture()
def search_env(tmp_path):
    """临时库: fund_etf_daily(code 列, 4 只) + index_daily(symbol 列) +
    macro_us_test(无代码列) + meta_asset_info(含一只表内不存在的 code)"""
    db = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{db}")
    repo = DataRepository(eng)
    repo.create_tables()
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE fund_etf_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, close TEXT, code TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE index_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, close TEXT, symbol TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE macro_us_test (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, value TEXT, created_at TEXT)'))
        for code, name in FUND_CODES.items():
            for d, close in (("2026-08-01", "10.0"), ("2026-08-02", "10.5")):
                c.execute(text(
                    'INSERT INTO fund_etf_daily (date, close, code) VALUES (:d, :c, :code)'),
                    {"d": d, "c": close, "code": code})
        c.execute(text(
            'INSERT INTO index_daily (date, close, symbol) VALUES '
            '("2026-08-01", "3200.0", "sh000001"), ("2026-08-02", "3210.0", "sh000001")'))
        c.execute(text(
            'INSERT INTO macro_us_test (date, value) VALUES '
            '("2026-08-01", "100.0"), ("2026-08-02", "101.0")'))
        # meta_asset_info: 真实 4 只 + 1 只表内不存在的(防污染)
        c.execute(text(
            'CREATE TABLE meta_asset_info (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'code TEXT, name TEXT, asset_type TEXT, created_at TEXT)'))
        for code, name in FUND_CODES.items():
            c.execute(text(
                'INSERT INTO meta_asset_info (code, name, asset_type) VALUES (:c, :n, "fund")'),
                {"c": code, "n": name})
        c.execute(text(
            'INSERT INTO meta_asset_info (code, name, asset_type) VALUES (:c, :n, "fund")'),
            {"c": FAKE_CODE, "n": "伪造基金"})
    server = FastAPIServer(repo=repo)
    yield TestClient(server.app)
    eng.dispose()
    if os.path.exists(db):
        os.remove(db)


def _codes(body):
    return [it["code"] for it in body["data"]]


def test_search_by_code_prefix(search_env):
    """代码前缀命中, matched_by=code, 稳定排序"""
    r = search_env.get("/api/v1/search", params={"q": "5103", "table": "fund_etf_daily"})
    assert r.status_code == 200
    body = r.json()
    assert _codes(body) == ["510300"]
    assert body["meta"]["matched_by"] == "code"


def test_search_by_chinese_name(search_env):
    """中文名子串命中, matched_by=name"""
    r = search_env.get("/api/v1/search", params={"q": "沪深", "table": "fund_etf_daily"})
    assert r.status_code == 200
    body = r.json()
    assert "510300" in _codes(body)
    assert body["meta"]["matched_by"] == "name"


def test_search_only_returns_codes_in_table(search_env):
    """meta_asset_info 里表内不存在的 code 不得出现在结果(搜到的必有数据)"""
    r = search_env.get("/api/v1/search", params={"q": FAKE_CODE, "table": "fund_etf_daily"})
    assert r.status_code == 200
    assert FAKE_CODE not in _codes(r.json())
    # 按名称搜伪造基金也不得命中
    r2 = search_env.get("/api/v1/search", params={"q": "伪造", "table": "fund_etf_daily"})
    assert FAKE_CODE not in _codes(r2.json())


def test_search_no_code_column_returns_empty(search_env):
    """无代码列表 → 200 空 + meta.code_column=null(只读发现, 空态比 422 友好)"""
    r = search_env.get("/api/v1/search", params={"table": "macro_us_test"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []
    assert body["meta"]["code_column"] is None


def test_search_empty_q_returns_first_n(search_env):
    """空 q → 全量 code 缓存后切片, 返回前 limit 个(下拉初始化候选)"""
    r = search_env.get("/api/v1/search", params={"table": "fund_etf_daily", "limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == len(FUND_CODES)
    assert body["meta"]["code_column"] == "code"


def test_search_limit_truncates(search_env):
    r = search_env.get("/api/v1/search", params={"table": "fund_etf_daily", "limit": 2})
    assert r.status_code == 200
    assert len(r.json()["data"]) == 2


def test_search_symbol_column_table(search_env):
    """index_daily 的代码列是 symbol, 搜索正常返回"""
    r = search_env.get("/api/v1/search", params={"q": "sh000", "table": "index_daily"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["code_column"] == "symbol"
    assert "sh000001" in _codes(body)


def test_search_illegal_table_404(search_env):
    r = search_env.get("/api/v1/search", params={"q": "5103", "table": "no_such_table"})
    assert r.status_code == 404
