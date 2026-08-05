# -*- coding: utf-8 -*-
"""数据源新鲜度逻辑测试"""

from datetime import date

import pytest

from src.core.freshness import (
    get_expected_interval_days,
    get_stale_status,
    collect_source_freshness,
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
