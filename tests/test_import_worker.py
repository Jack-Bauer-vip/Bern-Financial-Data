"""导入 worker 回归测试 — 防止「worker GC / match_table 参数不匹配」复发"""

import os
import tempfile

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.db.repository import DataRepository
from src.importer.matcher import match_table


@pytest.fixture()
def repo():
    """临时库，含 index_daily（英文行情）与一张中文宏观表"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    r = DataRepository(eng)
    with eng.begin() as c:
        c.execute(text(
            'CREATE TABLE index_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"date" TEXT, "open" TEXT, "high" TEXT, "low" TEXT, "close" TEXT, '
            '"volume" TEXT, created_at TEXT)'))
        c.execute(text(
            'CREATE TABLE macro_china_cpi_yearly ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, "商品" TEXT, "日期" TEXT, '
            '"今值" TEXT, "预测值" TEXT, "前值" TEXT, created_at TEXT)'))
    yield r
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_match_table_accepts_worker_kwargs(repo):
    """回归：worker 传入 tables_meta/filename 不应 TypeError（曾导致识别失败）"""
    df = pd.DataFrame({"日期": ["2026-07-01"], "开盘": ["4.1"], "收盘": ["4.2"]})
    # 显式传 tables_meta=None + filename → 走内部 collect_tables
    res = match_table(df, repo, ai_client=None,
                      tables_meta=None, filename="etf.csv")
    assert res is not None
    assert isinstance(res.table_name, str)


def test_match_table_with_precollected_tables(repo):
    """预收集候选表 + filename 传入"""
    from src.importer.matcher import collect_tables
    tables = collect_tables(repo)
    df = pd.DataFrame({"date": ["2026-01-01"], "open": ["1"], "close": ["2"]})
    res = match_table(df, repo, ai_client=None,
                      tables_meta=tables, filename="index.csv")
    assert res is not None
    assert res.table_name == "index_daily"


def test_match_table_english_quote_rule(repo):
    """英文行情列 → 规则命中 index_daily（无需 AI）"""
    df = pd.DataFrame({"date": ["2026-01-01"], "open": ["1"], "high": ["2"],
                       "low": ["0.5"], "close": ["1.5"], "volume": ["100"]})
    res = match_table(df, repo, ai_client=None)
    assert res.table_name == "index_daily"
    assert res.method == "rules"


def test_match_table_ai_fallback(repo):
    """中文行情列（日期/开盘/收盘）规则低置信 → 有 AI 时走 AI 路径"""
    class _FakeAI:
        def is_available(self):
            return True

        def identify_table(self, columns, sample_values, tables_desc):
            return {"table": "index_daily", "confidence": 0.9,
                    "reason": "行情列匹配"}

    df = pd.DataFrame({"日期": ["2026-07-01"], "开盘": ["4.1"], "收盘": ["4.2"]})
    res = match_table(df, repo, ai_client=_FakeAI())
    assert res.table_name == "index_daily"
    assert res.method == "ai"
    assert res.is_ai is True


def test_match_table_ai_rejected_if_unknown(repo):
    """AI 返回非法表名 → 拒绝，退回规则"""
    class _BadAI:
        def is_available(self):
            return True

        def identify_table(self, columns, sample_values, tables_desc):
            return {"table": "not_a_real_table", "confidence": 0.99}

    df = pd.DataFrame({"日期": ["2026-07-01"], "今值": ["3.5"]})
    res = match_table(df, repo, ai_client=_BadAI())
    # AI 表名非法 → 退回规则 top1
    assert res.table_name != "not_a_real_table"
    assert res.table_name in ("macro_china_cpi_yearly", "index_daily")
