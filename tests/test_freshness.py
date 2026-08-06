# -*- coding: utf-8 -*-
"""数据源新鲜度逻辑测试"""

from datetime import date, datetime, timedelta

import pytest

import pandas as pd

from src.core.freshness import (
    get_expected_interval_days,
    get_stale_status,
    collect_source_freshness,
    infer_expected_days_from_dates,
    is_heartbeat_stale,
    running_heartbeat_label,
    HEARTBEAT_STALE_MINUTES,
)


# ---------------------------------------------------------------------------
# get_expected_interval_days
# ---------------------------------------------------------------------------


def test_expected_interval_known_cron():
    assert get_expected_interval_days("0 22 * * 1-5") == 1.5
    assert get_expected_interval_days("0 10 * * *") == 1


def test_expected_interval_unknown_and_empty():
    assert get_expected_interval_days("1 2 3 4 5") == 7  # 未知 → 默认 7
    assert get_expected_interval_days("") == 7
    assert get_expected_interval_days(None) == 7


# ---------------------------------------------------------------------------
# get_stale_status
# ---------------------------------------------------------------------------


def test_stale_never_synced():
    label, color, text = get_stale_status(None, "0 10 * * *", today=date(2026, 8, 5))
    assert label == "未同步"


def test_stale_normal():
    label, _, _ = get_stale_status(date(2026, 8, 4), "0 10 * * *", today=date(2026, 8, 5))
    assert label == "✅ 正常"


def test_stale_lagging():
    # 日频，预期 1 天；stale_threshold = max(2, 3) = 3。4 天前 → 滞后
    label, _, _ = get_stale_status(date(2026, 8, 1), "0 10 * * *", today=date(2026, 8, 5))
    assert label == "⚠️ 滞后"


def test_stale_discontinued():
    # 20 天前，日频 → 停更
    label, _, _ = get_stale_status(date(2026, 7, 16), "0 10 * * *", today=date(2026, 8, 5))
    assert label == "🔴 停更"


def test_stale_future_data():
    label, _, _ = get_stale_status(date(2026, 8, 6), "0 10 * * *", today=date(2026, 8, 5))
    assert label == "✅ 正常"


# ---------------------------------------------------------------------------
# 实际数据频率推断（信号2：FRED 月频不误报"停更"）
# ---------------------------------------------------------------------------


def test_infer_expected_days_frequencies():
    # 月频（月末序列）→ 32 天
    monthly = pd.to_datetime(["2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30"])
    assert infer_expected_days_from_dates(monthly) == 32
    # 日频 → 2 天
    daily = pd.date_range("2026-08-01", periods=5, freq="D")
    assert infer_expected_days_from_dates(daily) == 2
    # 季频 → 95 天
    q = pd.to_datetime(["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"])
    assert infer_expected_days_from_dates(q) == 95
    # 不足 2 点 → None
    assert infer_expected_days_from_dates(pd.to_datetime(["2026-06-30"])) is None


def test_get_stale_status_expected_days_override():
    """FRED 月频：cron 是工作日但数据月频 → expected_days=32 不误报"""
    today = date(2026, 8, 5)
    # 65 天前的最新月频数据：按 cron(1.5天) 推断会判"停更"，按月频(32天) 应"滞后"
    last = date(2026, 6, 1)
    label_cron = get_stale_status(last, "0 22 * * 1-5", today)[0]
    label_actual = get_stale_status(last, "0 22 * * 1-5", today, expected_days=32)[0]
    assert label_cron == "🔴 停更"      # 旧逻辑：误报
    assert label_actual != "🔴 停更"     # 新逻辑：不误报（滞后）
    assert label_actual == "⚠️ 滞后"


def test_get_stale_status_monthly_normal():
    """月频 40 天前（含发布延迟）→ expected_days=32 判正常"""
    label = get_stale_status(date(2026, 6, 26), "0 22 * * 1-5",
                             date(2026, 8, 5), expected_days=32)
    assert label[0] == "✅ 正常"


# ---------------------------------------------------------------------------
# collect_source_freshness — 跳过 deprecated
# ---------------------------------------------------------------------------


class _FakeSyncJob:
    def __init__(self, last_sync_date):
        self.last_sync_date = last_sync_date


class _FakeRepo:
    def get_sync_job(self, module_name):
        jobs = {
            "macro_ok": _FakeSyncJob(date(2026, 8, 4)),
        }
        return jobs.get(module_name)

    def table_exists(self, table_name):
        return table_name in ("macro_ok", "macro_dep")

    def get_last_date(self, table_name, col):
        if table_name == "macro_ok":
            return date(2026, 8, 4)
        return None


class _FakeRegistry:
    def get_all_sources(self, include_deprecated=True):
        return [
            {"source_key": "macro.ok", "name": "正常源", "table_name": "macro_ok",
             "api_function": "x", "schedule_cron": "0 10 * * *"},
            {"source_key": "macro.dep", "name": "废弃源", "table_name": "macro_dep",
             "api_function": "y", "schedule_cron": "0 22 * * 1-5", "deprecated": True},
        ]


def test_collect_skips_deprecated_by_default():
    rows = collect_source_freshness(_FakeRepo(), _FakeRegistry(), date(2026, 8, 5))
    keys = [r.source_key for r in rows]
    assert "macro.ok" in keys
    assert "macro.dep" not in keys  # deprecated 默认跳过


def test_collect_includes_deprecated_when_requested():
    rows = collect_source_freshness(
        _FakeRepo(), _FakeRegistry(), date(2026, 8, 5), include_deprecated=True)
    keys = [r.source_key for r in rows]
    assert "macro.dep" in keys
    dep = next(r for r in rows if r.source_key == "macro.dep")
    assert dep.deprecated is True


def test_collect_status_fields():
    rows = collect_source_freshness(_FakeRepo(), _FakeRegistry(), date(2026, 8, 5))
    ok = next(r for r in rows if r.source_key == "macro.ok")
    assert ok.status_label == "✅ 正常"
    assert ok.status_color.startswith("#")
    assert ok.days_text.endswith("天前")


# ---------------------------------------------------------------------------
# P1 长任务心跳 / 僵死检测
# ---------------------------------------------------------------------------


def test_is_heartbeat_stale_cases():
    now = datetime(2026, 8, 6, 12, 0, 0)
    # 非 running → 永不失活
    assert is_heartbeat_stale("idle", None, now) is False
    assert is_heartbeat_stale("", None, now) is False
    # running 但从未心跳 → 疑似僵死
    assert is_heartbeat_stale("running", None, now) is True
    # running + 心跳过期（> 10 分钟）
    old = now - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
    assert is_heartbeat_stale("running", old, now) is True
    # running + 心跳新鲜
    fresh = now - timedelta(minutes=HEARTBEAT_STALE_MINUTES - 1)
    assert is_heartbeat_stale("running", fresh, now) is False


def test_running_heartbeat_label_cases():
    now = datetime(2026, 8, 6, 12, 0, 0)
    assert running_heartbeat_label("idle", None, now) == (None, None)
    assert running_heartbeat_label("running", None, now)[0] == "疑似僵死"
    fresh = now - timedelta(minutes=1)
    assert running_heartbeat_label("running", fresh, now) == ("运行中", "#1e88e5")
    stale = now - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 5)
    assert running_heartbeat_label("running", stale, now) == ("疑似僵死", "#8b0000")


class _FakeRepoRunning(_FakeRepo):
    """带 running 状态的假 repo（fund_etf_daily 任务 running + 心跳过期）"""
    def get_sync_job(self, module_name):
        if module_name == "fund_etf_daily":
            job = _FakeSyncJob(date(2026, 8, 6))
            job.running_status = "running"
            job.last_heartbeat = datetime.now() - timedelta(minutes=30)
            return job
        return super().get_sync_job(module_name)

    def table_exists(self, table_name):
        return table_name == "fund_etf_daily" or super().table_exists(table_name)


class _FakeRegistryRunning(_FakeRegistry):
    def get_all_sources(self, include_deprecated=True):
        srcs = super().get_all_sources(include_deprecated=include_deprecated)
        srcs.append({
            "source_key": "fund.etf_daily", "name": "ETF基金日线",
            "table_name": "fund_etf_daily", "api_function": "fund_daily",
            "schedule_cron": "0 22 * * 1-5",
        })
        return srcs


def test_collect_surfaces_running_heartbeat():
    """running 且心跳过期的任务 → 新鲜度快照带 running 字段，标签为疑似僵死"""
    rows = collect_source_freshness(
        _FakeRepoRunning(), _FakeRegistryRunning(), date(2026, 8, 6))
    fund = next(r for r in rows if r.source_key == "fund.etf_daily")
    assert fund.running_status == "running"
    assert fund.last_heartbeat is not None
    label, color = running_heartbeat_label(fund.running_status, fund.last_heartbeat)
    assert label == "疑似僵死" and color == "#8b0000"


class _FakeRepoMonthly:
    """FRED 月频源的假 repo：表内月末数据，最近 8 个日期月间隔"""
    def get_sync_job(self, module_name):
        return None

    def table_exists(self, table_name):
        return table_name == "macro_fred_unemployment"

    def get_last_date(self, table_name, col):
        if table_name == "macro_fred_unemployment":
            return date(2026, 6, 1)  # 65 天前
        return None

    def get_all_existing_columns(self, table_name):
        return ["id", "date", "value"]

    def query(self, table_name, order_by=None, limit=8, **kw):
        # 月末月频序列
        return pd.DataFrame({
            "date": ["2026-01-01", "2026-02-01", "2026-03-01",
                     "2026-04-01", "2026-05-01", "2026-06-01"],
        })


class _FakeRegistryMonthly:
    def get_all_sources(self, include_deprecated=True):
        return [{
            "source_key": "macro.us.fred.unemployment", "name": "美国失业率(FRED)",
            "table_name": "macro_fred_unemployment",
            "api_function": "UNRATE", "schedule_cron": "0 22 * * 1-5",
        }]


def test_collect_uses_actual_frequency_for_monthly():
    """FRED 月频源：按实际频率(月频)判滞后而非停更——修复误报"""
    rows = collect_source_freshness(
        _FakeRepoMonthly(), _FakeRegistryMonthly(), date(2026, 8, 5))
    assert len(rows) == 1
    r = rows[0]
    assert r.status_label == "⚠️ 滞后"   # 不再误报"停更"
    assert r.status_label != "🔴 停更"
