# -*- coding: utf-8 -*-
"""基金日线批量同步测试 — run_fund_daily_batch / _prepare_fund_batch_df

回归：基金同步原本逐个 code（1000 次调用 ~37min），批量改为按交易日
（trade_cal + 逐日 fund_daily(trade_date=) 全市场拉取，补 N 天只需 N 次调用）。
"""

import os
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.core.sync_engine import SyncEngine
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.fetcher_registry import FetcherRegistry
from src.core.data_fetcher import DataFetcher
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


# ---------------------------------------------------------------------------
# _prepare_fund_batch_df — tushare 全市场返回 → 目标表规范列
# ---------------------------------------------------------------------------


def test_prepare_fund_batch_df_maps_columns():
    """ts_code→code(去后缀)、trade_date→date(ISO)、vol→volume；丢弃多余列"""
    df = pd.DataFrame({
        "ts_code": ["159915.SZ", "510050.SH"],
        "trade_date": ["20260804", "20260804"],
        "pre_close": [1.0, 2.0],
        "open": [3.0, 4.0],
        "high": [5.0, 6.0],
        "low": [1.0, 2.0],
        "close": [7.0, 8.0],
        "change": [0.1, 0.2],
        "pct_chg": [1.1, 1.2],
        "vol": [100.0, 200.0],
        "amount": [1000.0, 2000.0],
    })
    out = SyncEngine._prepare_fund_batch_df(df)
    assert list(out.columns) == ["date", "open", "high", "low", "close",
                                 "volume", "amount", "code"]
    assert out["code"].tolist() == ["159915", "510050"]
    assert out["date"].tolist() == ["2026-08-04", "2026-08-04"]
    assert out["volume"].tolist() == [100.0, 200.0]


# ---------------------------------------------------------------------------
# run_fund_daily_batch — mock tushare
# ---------------------------------------------------------------------------


class FakeFetcher:
    """只提供 tushare_pro 的假 fetcher"""
    def __init__(self, pro):
        self._pro = pro

    @property
    def tushare_pro(self):
        return self._pro


class FakePro:
    def __init__(self, trade_days, day_rows):
        self._trade_days = trade_days          # ["20260803", ...]
        self._day_rows = day_rows              # {trade_date: DataFrame}

    def trade_cal(self, exchange, start_date, end_date, is_open):
        days = [d for d in self._trade_days if start_date <= d <= end_date]
        return pd.DataFrame({"cal_date": days})

    def fund_daily(self, trade_date):
        return self._day_rows.get(trade_date, pd.DataFrame())


def _make_day(trade_date: str, n: int = 3) -> pd.DataFrame:
    """构造一个交易日的全市场假数据（n 只基金）"""
    codes = [f"{100000 + i:06d}.SZ" for i in range(n)]
    return pd.DataFrame({
        "ts_code": codes,
        "trade_date": [trade_date] * n,
        "pre_close": [1.0] * n,
        "open": [1.0] * n,
        "high": [1.0] * n,
        "low": [1.0] * n,
        "close": [1.0] * n,
        "change": [0.0] * n,
        "pct_chg": [0.0] * n,
        "vol": [10.0] * n,
        "amount": [100.0] * n,
    })


@pytest.fixture()
def batch_env():
    """临时库 + 真 SyncEngine（fetcher 用 fake pro）"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    repo = DataRepository(eng)
    repo.create_tables()
    config = ConfigManager()

    def _build(pro, seed_table=True):
        if seed_table:
            # 预置 fund_etf_daily 表 + 一行旧数据（表内 max = today-3）
            schema = DynamicSchemaManager(repo)
            old = pd.DataFrame({
                "date": [(date.today() - timedelta(days=3)).isoformat()],
                "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                "volume": [10.0], "amount": [100.0], "code": ["159915"],
            })
            schema.ensure_columns("fund_etf_daily", list(old.columns))
            repo.bulk_upsert("fund_etf_daily", old, unique_columns=["code", "date"])
        engine = SyncEngine(FakeFetcher(pro), repo, DynamicSchemaManager(repo), config)
        return engine, repo

    yield _build
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_run_fund_daily_batch_writes_all_trade_days(batch_env):
    """补 2 个交易日，全市场数据入库，sync job 更新为 completed"""
    today = date.today()
    d1 = (today - timedelta(days=2)).strftime("%Y%m%d")
    d2 = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d1, d2], {d1: _make_day(d1, 3), d2: _make_day(d2, 4)})
    engine, repo = batch_env(pro)

    total = engine.run_fund_daily_batch()
    # 本次写入 = d1 3 行 + d2 4 行 = 7（全市场入库，不限于已存在的 159915）
    assert total == 7

    rows = repo.query("fund_etf_daily")
    # 预置旧行 1 行 + 本次 7 行 = 8 行（日期不重叠，(code,date) 唯一键去重）
    assert len(rows) == 8
    codes = set(rows["code"])
    assert "159915" in codes and "100000" in codes

    job = repo.get_sync_job("fund_etf_daily")
    assert job is not None and job.status == "completed"
    assert job.error_message is None


def test_run_fund_daily_batch_up_to_date_skips(batch_env):
    """表内 max = 今天 → start > end → 直接返回 0 不调 tushare"""
    today = date.today()
    pro = FakePro([today.strftime("%Y%m%d")], {})
    engine, repo = batch_env(pro)
    # 手动把表内 max 顶到今天
    with repo.engine.connect() as conn:
        conn.execute(text(
            f'UPDATE fund_etf_daily SET date = "{today.isoformat()}"'))
        conn.commit()

    total = engine.run_fund_daily_batch()
    assert total == 0


def test_run_fund_daily_batch_first_run_without_seed(batch_env):
    """表内无数据（无 fund_etf_daily 表）→ 首次回补近 1 年，仍能建表入库"""
    today = date.today()
    d = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d], {d: _make_day(d, 2)})
    engine, repo = batch_env(pro, seed_table=False)

    total = engine.run_fund_daily_batch()
    assert total == 2
    assert repo.table_exists("fund_etf_daily")
    assert len(repo.query("fund_etf_daily")) == 2
