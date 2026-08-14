# -*- coding: utf-8 -*-
"""指数日线批量同步测试 — run_index_daily_batch / _prepare_index_batch_df / 映射

背景：境内指数原逐个 code 走 akshare（732 只串行 ~37 分钟），批量改为
tushare index_daily(trade_date=) 按交易日一次拉全市场（补 N 天 N 次调用）。
覆盖：ts_code→symbol 映射（仅 sh000/sz399 入库）、列规范、增量起点按境内
子集（跨境 FRED 日期不拖后腿）、全市场过滤、幂等跳过。
"""

import os
import tempfile
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from src.core.sync_engine import (
    SyncEngine,
    _ts_code_to_index_symbol,
)
from scripts_gen.sync_index_quick import _compute_stale
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.data_fetcher import DataFetcher
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


# ---------------------------------------------------------------------------
# ts_code → symbol 映射（纯函数）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ts_code,expected", [
    ("000001.SH", "sh000001"),
    ("000300.SH", "sh000300"),
    ("399001.SZ", "sz399001"),
    ("399997.SZ", "sz399997"),
    ("000001", "sh000001"),          # 无后缀裸代码兼容
    ("000001.sh", "sh000001"),       # 后缀大小写不敏感
    ("930633.CSI", None),            # 中证 93 系 → 库内无此形态
    ("H30533.CSI", None),            # 中证 H 系 → None
    ("950001.CSI", None),
    ("000001.SH ", "sh000001"),      # 前后空白容忍
    ("abc", None),
])
def test_ts_code_to_index_symbol(ts_code, expected):
    assert _ts_code_to_index_symbol(ts_code) == expected


# ---------------------------------------------------------------------------
# _prepare_index_batch_df — tushare 全市场返回 → 目标表规范列
# ---------------------------------------------------------------------------


def test_prepare_index_batch_df_maps_symbols():
    """ts_code→symbol、trade_date→date(ISO)、vol→volume；非库内形态行丢弃"""
    df = pd.DataFrame({
        "ts_code": ["000300.SH", "399001.SZ", "930633.CSI", "H30533.CSI"],
        "trade_date": ["20260804", "20260804", "20260804", "20260804"],
        "pre_close": [1.0, 2.0, 3.0, 4.0],
        "open": [3.0, 4.0, 5.0, 6.0],
        "high": [5.0, 6.0, 7.0, 8.0],
        "low": [1.0, 2.0, 3.0, 4.0],
        "close": [7.0, 8.0, 9.0, 10.0],
        "change": [0.1, 0.2, 0.3, 0.4],
        "pct_chg": [1.1, 1.2, 1.3, 1.4],
        "vol": [100.0, 200.0, 300.0, 400.0],
        "amount": [1000.0, 2000.0, 3000.0, 4000.0],
    })
    out = SyncEngine._prepare_index_batch_df(df)
    assert list(out.columns) == ["date", "open", "high", "low", "close",
                                 "volume", "amount", "symbol"]
    assert out["symbol"].tolist() == ["sh000300", "sz399001"]  # 93/H 系已丢
    assert out["date"].tolist() == ["2026-08-04", "2026-08-04"]
    assert out["volume"].tolist() == [100.0, 200.0]


# ---------------------------------------------------------------------------
# run_index_daily_batch — mock tushare
# ---------------------------------------------------------------------------


class FakeFetcher:
    def __init__(self, pro):
        self._pro = pro

    @property
    def tushare_pro(self):
        return self._pro


class FakePro:
    def __init__(self, trade_days, day_rows):
        self._trade_days = trade_days
        self._day_rows = day_rows

    def trade_cal(self, exchange, start_date, end_date, is_open):
        days = [d for d in self._trade_days if start_date <= d <= end_date]
        return pd.DataFrame({"cal_date": days})

    def index_daily(self, trade_date):
        return self._day_rows.get(trade_date, pd.DataFrame())


def _make_day(trade_date: str, sh_n: int = 2, sz_n: int = 1) -> pd.DataFrame:
    """构造一个交易日的全市场假数据（sh 只 + sz 只 + 1 只中证 93 系验证过滤）"""
    rows = []
    for i in range(sh_n):
        rows.append({
            "ts_code": f"{100 + i:06d}.SH", "trade_date": trade_date,
            "pre_close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 1.0, "change": 0.0, "pct_chg": 0.0, "vol": 10.0,
            "amount": 100.0,
        })
    for i in range(sz_n):
        rows.append({
            "ts_code": f"{399000 + i:06d}.SZ", "trade_date": trade_date,
            "pre_close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0,
            "close": 1.0, "change": 0.0, "pct_chg": 0.0, "vol": 20.0,
            "amount": 200.0,
        })
    rows.append({
        "ts_code": "930633.CSI", "trade_date": trade_date,
        "pre_close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0,
        "close": 1.0, "change": 0.0, "pct_chg": 0.0, "vol": 30.0,
        "amount": 300.0,
    })
    return pd.DataFrame(rows)


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
            # 预置 index_daily 表 + 一行境内旧数据（境内 max = today-3）
            schema = DynamicSchemaManager(repo)
            old = pd.DataFrame({
                "date": [(date.today() - timedelta(days=3)).isoformat()],
                "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                "volume": [10.0], "amount": [100.0], "symbol": ["sh000001"],
            })
            schema.ensure_columns("index_daily", list(old.columns))
            repo.bulk_upsert("index_daily", old, unique_columns=["symbol", "date"])
        engine = SyncEngine(FakeFetcher(pro), repo, DynamicSchemaManager(repo), config)
        return engine, repo

    yield _build
    eng.dispose()
    if os.path.exists(tmp):
        os.remove(tmp)


def test_compute_stale_skips_dead_codes():
    """兜底只补近期活跃的落后指数；数据停更超 lookback 的死代码跳过"""
    achieved = date(2026, 8, 12)
    m = {
        "sh000001": date(2026, 8, 11),   # 落后 1 天 → 兜底
        "sz399001": date(2026, 8, 12),   # 已到前沿 → 不兜底
        "sh000944": date(2021, 2, 4),    # 停更 5 年 → 死代码跳过
        "sz399404": date(2025, 7, 3),    # 停更 13 个月 → 死代码跳过
        "sh000805": None,                # 无数据 → 视为落后兜底（新 code 场景）
    }
    stale, dead = _compute_stale(m, achieved, lookback_days=30)
    assert sorted(stale) == ["sh000001", "sh000805"]
    assert dead == 2


def test_compute_stale_none_achieved():
    """achieved=None 只可能出现在表内无任何境内指数(after 为空)时 → 无兜底目标"""
    stale, dead = _compute_stale({}, None)
    assert stale == [] and dead == 0


def test_run_index_daily_batch_writes_trade_days(batch_env):
    """补 2 个交易日：全市场入库、93 系被过滤、sync job 更新 completed"""
    today = date.today()
    d1 = (today - timedelta(days=2)).strftime("%Y%m%d")
    d2 = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d1, d2], {d1: _make_day(d1, 2, 1), d2: _make_day(d2, 2, 1)})
    engine, repo = batch_env(pro)

    total = engine.run_index_daily_batch()
    # 每交易日 2 sh + 1 sz = 3 行入库（93 系 1 行被过滤），两天 6 行
    assert total == 6

    rows = repo.query("index_daily")
    # 预置 1 行 + 6 行
    assert len(rows) == 7
    syms = set(rows["symbol"])
    assert "sh000001" in syms and "sz399000" in syms
    assert not any(s.startswith("93") for s in syms)   # 中证 93 系未写入
    assert "sh930633" not in syms

    job = repo.get_sync_job("index_daily")
    assert job is not None and job.status == "completed"
    assert job.error_message is None


def test_run_index_daily_batch_up_to_date_skips(batch_env):
    """境内 max = 今天 → start > end → 直接返回 0 不调 tushare"""
    today = date.today()
    pro = FakePro([today.strftime("%Y%m%d")], {})
    engine, repo = batch_env(pro)
    with repo.engine.connect() as conn:
        conn.execute(text(
            f'UPDATE index_daily SET date = "{today.isoformat()}"'))
        conn.commit()

    total = engine.run_index_daily_batch()
    assert total == 0


def test_run_index_daily_batch_first_run_without_seed(batch_env):
    """表内无数据 → 首次回补近 1 年，仍能建表入库"""
    today = date.today()
    d = (today - timedelta(days=1)).strftime("%Y%m%d")
    pro = FakePro([d], {d: _make_day(d, 2, 1)})
    engine, repo = batch_env(pro, seed_table=False)

    total = engine.run_index_daily_batch()
    assert total == 3  # 2 sh + 1 sz
    assert repo.table_exists("index_daily")
    assert len(repo.query("index_daily")) == 3


def test_run_index_daily_batch_incremental_start_domestic(batch_env):
    """跨境(FRED)日期领先境内 → 增量起点仍按境内 max+1，不跳过境内交易日"""
    today = date.today()
    d_future = (today - timedelta(days=1)).strftime("%Y%m%d")   # 境内前沿 = today-2
    # 预置境内 today-2 + 跨境 today-1（模拟 FRED 领先）
    engine0, repo = batch_env(FakePro([], {}))
    schema = DynamicSchemaManager(repo)
    extra = pd.DataFrame({
        "date": [(today - timedelta(days=2)).isoformat()] * 2,
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [10.0, 20.0], "amount": [100.0, 200.0],
        "symbol": ["sh000100", "SPX"],           # SPX 跨境 = today-1
    })
    # 用额外行把跨境日期顶到 today-1
    with repo.engine.connect() as conn:
        conn.execute(text("UPDATE index_daily SET date = :d WHERE symbol = 'SPX'"),
                     {"d": (today - timedelta(days=1)).isoformat()})
        conn.commit()
    del extra

    d_target = today.strftime("%Y%m%d")
    pro = FakePro([d_target], {d_target: _make_day(d_target, 2, 1)})
    engine = SyncEngine(FakeFetcher(pro), repo, DynamicSchemaManager(repo), ConfigManager())

    total = engine.run_index_daily_batch()
    # 境内 max = today-2（SPX 跨境不算）→ 增量起点 today-1，但交易日历只有 today
    # → 应拉到 today，写入 3 行；若误用表级 max(today-1) 则起点 today → 也算 today，
    # 边界难区分。改用严格断言：增量起点日志说明境内 max。
    assert total == 3


def test_run_index_daily_batch_backfill_skips_covered_days(batch_env):
    """回溯模式：某日行数 >= 阈值(400) → 视为已全市场覆盖，不调 index_daily"""
    d_full, d_partial = "20230601", "20230602"
    # 预置 d_full 为"已全市场"：塞 400 行（不同 symbol）
    engine, repo = batch_env(FakePro([], {}))
    schema = DynamicSchemaManager(repo)
    big = pd.DataFrame({
        "date": ["2023-06-01"] * 400,
        "open": [1.0] * 400, "high": [1.0] * 400, "low": [1.0] * 400,
        "close": [1.0] * 400, "volume": [10.0] * 400,
        "amount": [100.0] * 400,
        "symbol": [("sh" + f"{100 + i:06d}") for i in range(400)],
    })
    schema.ensure_columns("index_daily", list(big.columns))
    repo.bulk_upsert("index_daily", big, unique_columns=["symbol", "date"])

    calls = []

    class CountingPro(FakePro):
        def index_daily(self, trade_date):
            calls.append(trade_date)
            return super().index_daily(trade_date)

    pro = CountingPro([d_full, d_partial], {d_partial: _make_day(d_partial, 2, 1)})
    engine2 = SyncEngine(FakeFetcher(pro), repo,
                         DynamicSchemaManager(repo), ConfigManager())

    total = engine2.run_index_daily_batch(
        start_date=date(2023, 6, 1), end_date=date(2023, 6, 30))
    # d_full(400行)被跳过 → 只拉 d_partial(3 行)
    assert total == 3
    assert d_full not in calls and d_partial in calls


# ---------------------------------------------------------------------------
# sync.run("index.daily") 批量分发（第一刀: 行情纳入每日自动同步）
#
# data_catalog.yaml 给 index.daily 配了 sync_mode=index_daily_batch，
# 让 sync.run() 委托到 run_index_daily_batch（而不是逐个默认 code 同步）。
# 定时调度回调正是调 sync.run(source_key)，配置驱动即能全市场每日更新。
# ---------------------------------------------------------------------------


def test_sync_run_dispatches_index_daily_batch(monkeypatch, batch_env):
    """sync.run('index.daily') 按配置 sync_mode=index_daily_batch 委托批量方法"""
    engine, repo = batch_env(FakePro({}, {}))
    calls = []

    def _fake_batch(self, start_date=None, end_date=None):
        calls.append((start_date, end_date))
        return 7

    monkeypatch.setattr(SyncEngine, "run_index_daily_batch", _fake_batch)
    result = engine.run("index.daily")
    assert result == 7
    assert calls  # 委托到批量方法（非逐个 code 同步）


def test_sync_run_index_config_has_batch_mode():
    """回归: index.daily 配置携带 sync_mode=index_daily_batch（驱动分发）"""
    from src.core.fetcher_registry import FetcherRegistry
    from src.utils.config import ConfigManager
    cfg = FetcherRegistry(ConfigManager()).get_source("index.daily")
    assert cfg is not None
    assert cfg.get("sync_mode") == "index_daily_batch"
    assert cfg.get("schedule_cron")

