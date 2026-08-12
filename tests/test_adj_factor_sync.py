# -*- coding: utf-8 -*-
"""复权因子批量同步测试 — run_adj_factor_batch / _prepare_adj_factor_df

复用基金批量同构模式（trade_cal + 逐日全市场拉取）。两 asset_type 写同一
asset_adj_factor 表，meta_sync_jobs 按 module 名分行，增量按 asset_type 的 MAX(date)。
"""

import os
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine

from src.core.sync_engine import SyncEngine
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.data_fetcher import DataFetcher
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


# ---------------------------------------------------------------------------
# _prepare_adj_factor_df — tushare 因子返回 → 规范列
# ---------------------------------------------------------------------------


def test_prepare_adj_factor_df_maps_columns():
    df = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": ["20260804", "20260804"],
        "adj_factor": [1.5, 2.0],
        "other": [1, 2],
    })
    out = SyncEngine._prepare_adj_factor_df(df, "stock")
    assert list(out.columns) == ["asset_type", "code", "date", "factor"]
    assert out["code"].tolist() == ["000001", "600000"]
    assert out["date"].tolist() == ["2026-08-04", "2026-08-04"]
    assert out["asset_type"].tolist() == ["stock", "stock"]
    assert out["factor"].tolist() == [1.5, 2.0]


def test_prepare_adj_factor_df_drops_nan_factor():
    df = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": ["20260804", "20260804"],
        "adj_factor": [1.5, None],
    })
    out = SyncEngine._prepare_adj_factor_df(df, "fund")
    assert len(out) == 1
    assert out["code"].tolist() == ["000001"]


# ---------------------------------------------------------------------------
# run_adj_factor_batch — mock tushare
# ---------------------------------------------------------------------------


class FakeFetcher:
    def __init__(self, pro):
        self._pro = pro

    @property
    def tushare_pro(self):
        return self._pro


class FakePro:
    """模拟 tushare pro：trade_cal + adj_factor/fund_adj 按 trade_date 返回"""

    def __init__(self, trade_days, day_rows, fail_days=None, perm_error_days=None):
        self._trade_days = trade_days
        self._day_rows = day_rows            # {trade_date: DataFrame}
        self._fail_days = fail_days or set()
        self._perm_error_days = perm_error_days or set()

    def trade_cal(self, exchange, start_date, end_date, is_open):
        days = [d for d in self._trade_days if start_date <= d <= end_date]
        return pd.DataFrame({"cal_date": days})

    def _row(self, func, trade_date):
        if trade_date in self._perm_error_days:
            raise Exception("抱歉，您没有权限（积分不足）")
        if trade_date in self._fail_days:
            raise Exception("网络错误")
        return self._day_rows.get(trade_date, pd.DataFrame())

    def adj_factor(self, trade_date):
        return self._row("adj_factor", trade_date)

    def fund_adj(self, trade_date):
        return self._row("fund_adj", trade_date)


def _make_factor_day(trade_date: str, n: int = 3, prefix: str = "000") -> pd.DataFrame:
    codes = [f"{prefix}{100 + i:03d}.SZ" for i in range(n)]
    return pd.DataFrame({
        "ts_code": codes,
        "trade_date": [trade_date] * n,
        "adj_factor": [1.0 + i for i in range(n)],
    })


@pytest.fixture()
def adj_env():
    """临时库 + 真 SyncEngine（fetcher 用 fake pro）"""
    tmp = tempfile.mktemp(suffix=".db")
    eng = create_engine(f"sqlite:///{tmp}")
    repo = DataRepository(eng)
    repo.create_tables()
    config = ConfigManager()

    def _build(pro):
        engine = SyncEngine(FakeFetcher(pro), repo, DynamicSchemaManager(repo), config)
        return engine, repo

    yield _build
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_run_adj_factor_batch_writes_all_trade_days(adj_env):
    """补 2 个交易日，因子入库 + asset_type 注入，sync job 分行 completed"""
    today = date.today()
    d1 = (today - timedelta(days=2)).strftime("%Y%m%d")
    d2 = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d1, d2], {d1: _make_factor_day(d1, 3), d2: _make_factor_day(d2, 4)})
    engine, repo = adj_env(pro)

    total = engine.run_adj_factor_batch("stock")
    assert total == 7  # 3 + 4

    rows = repo.query_adj_factors("stock", ["000100", "000101", "000102", "000103"])
    assert len(rows) == 7
    assert set(rows["asset_type"]) == {"stock"}
    assert set(rows["code"]) == {"000100", "000101", "000102", "000103"}

    job = repo.get_sync_job("asset_adj_factor_stock")
    assert job is not None and job.status == "completed"
    assert job.running_status == "idle"
    assert job.error_message is None


def test_run_adj_factor_batch_up_to_date_skips(adj_env):
    """因子已到最新 → start > end → 返回 0 不调 tushare"""
    today = date.today()
    pro = FakePro([today.strftime("%Y%m%d")], {})
    engine, repo = adj_env(pro)
    # 预置因子到最新
    schema = DynamicSchemaManager(repo)
    seed = pd.DataFrame({
        "asset_type": ["stock"], "code": ["000001"],
        "date": [today.isoformat()], "factor": [1.0],
    })
    schema.ensure_columns("asset_adj_factor", list(seed.columns))
    repo.bulk_upsert("asset_adj_factor", seed, unique_columns=["asset_type", "code", "date"])

    total = engine.run_adj_factor_batch("stock")
    assert total == 0


def test_run_adj_factor_batch_first_run_no_table(adj_env):
    """表不存在（无因子）→ 首跑回补近 1 年，自动建表入库"""
    today = date.today()
    d = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d], {d: _make_factor_day(d, 2)})
    engine, repo = adj_env(pro)

    total = engine.run_adj_factor_batch("fund")
    assert total == 2
    assert repo.table_exists("asset_adj_factor")
    assert len(repo.query_adj_factors("fund", ["000100", "000101"])) == 2
    job = repo.get_sync_job("asset_adj_factor_fund")
    assert job is not None and job.status == "completed"


def test_run_adj_factor_batch_asset_types_isolated(adj_env):
    """stock 与 fund 因子写同一表但 asset_type 隔离，增量起点互不污染"""
    today = date.today()
    d1 = (today - timedelta(days=2)).strftime("%Y%m%d")
    pro = FakePro([d1], {d1: _make_factor_day(d1, 3)})
    engine, repo = adj_env(pro)

    engine.run_adj_factor_batch("stock")
    total_fund = engine.run_adj_factor_batch("fund")
    # fund 无表内数据 → 首跑回补近 1 年，重新拉 d1
    assert total_fund == 3
    assert set(repo.query_adj_factors("stock", ["000100"])["asset_type"]) == {"stock"}
    assert set(repo.query_adj_factors("fund", ["000100"])["asset_type"]) == {"fund"}


def test_run_adj_factor_batch_permission_error_aborts(adj_env):
    """fund_adj 权限不足 → 记错误、中止，不静默逐 code 跑 akshare"""
    today = date.today()
    d1 = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d1], {d1: _make_factor_day(d1, 2)}, perm_error_days={d1})
    engine, repo = adj_env(pro)

    engine.run_adj_factor_batch("fund")
    job = repo.get_sync_job("asset_adj_factor_fund")
    assert job is not None
    assert job.error_message is not None
    assert "权限" in (job.error_message or "")
    # 无数据写入
    assert len(repo.query_adj_factors("fund", ["000100"])) == 0


def test_run_adj_factor_batch_failed_day_continues(adj_env):
    """单日网络错误 → 记 failed 继续后续交易日，不中断"""
    today = date.today()
    d1 = (today - timedelta(days=2)).strftime("%Y%m%d")
    d2 = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d1, d2], {d1: _make_factor_day(d1, 3), d2: _make_factor_day(d2, 4)},
                  fail_days={d1})
    engine, repo = adj_env(pro)

    total = engine.run_adj_factor_batch("stock")
    assert total == 4  # 只有 d2 成功
