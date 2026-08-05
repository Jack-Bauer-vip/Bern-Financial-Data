# -*- coding: utf-8 -*-
"""数据源新鲜度 — 纯函数，供 GUI HealthDialog / API /health / CLI 脚本三方共用

从 src/gui/dialogs/health_dialog.py 拆出（health_dialog 顶部 import PySide6，
CLI 脚本不能 import 它，故纯逻辑独立成模块）。
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional


# Cron 表达式到预期更新间隔（天）的映射
CRON_TO_EXPECTED_DAYS = {
    # 每天
    "0 0 * * *": 1, "0 6 * * *": 1, "0 8 * * *": 1,
    "0 10 * * *": 1, "0 14 * * *": 1, "0 15 * * *": 1,
    "0 22 * * *": 1,
    # 工作日
    "0 0 * * 1-5": 1.5, "0 8 * * 1-5": 1.5,
    "0 10 * * 1-5": 1.5, "0 14 * * 1-5": 1.5,
    "0 15 * * 1-5": 1.5, "0 18 * * 1-5": 1.5,
    "0 22 * * 1-5": 1.5, "30 18 * * 1-5": 1.5,
    "30 19 * * 1-5": 1.5,
    # 其他
    "0 */6 * * *": 0.25,
    "0 */12 * * *": 0.5,
}

DEFAULT_EXPECTED_DAYS = 7  # 默认认为 7 天内算正常


def get_expected_interval_days(cron: str) -> float:
    """从 cron 表达式推算预期更新间隔（天）"""
    if not cron:
        return DEFAULT_EXPECTED_DAYS
    cron = cron.strip()
    return CRON_TO_EXPECTED_DAYS.get(cron, DEFAULT_EXPECTED_DAYS)


def get_stale_status(
    last_date: Optional[date],
    cron: str,
    today: date = None,
) -> tuple[str, str, str]:
    """判断数据源健康状态

    Returns:
        (status_label, status_color_hex, days_since_update_str)
    """
    if today is None:
        today = date.today()

    if last_date is None:
        return ("未同步", "#888888", "从未同步")

    days_since = (today - last_date).days
    expected_days = get_expected_interval_days(cron)
    # 松一点：允许 2 倍预期间隔
    stale_threshold = max(expected_days * 2, 3)

    if days_since < 0:
        return ("✅ 正常", "#2e7d32", f"未来数据 ({days_since}天)")
    elif days_since <= stale_threshold:
        return ("✅ 正常", "#2e7d32", f"{days_since} 天前")
    elif days_since <= stale_threshold * 2:
        return ("⚠️ 滞后", "#e65100", f"{days_since} 天前")
    else:
        return ("🔴 停更", "#c62828", f"{days_since} 天前")


@dataclass
class SourceFreshness:
    """单个数据源的新鲜度快照"""
    source_key: str
    name: str
    table_name: str
    api_function: str
    cron: str
    last_date: date | None
    status_label: str
    status_color: str
    days_text: str
    days_since: int | None
    deprecated: bool = False


def collect_source_freshness(
    repo,
    registry,
    today: date = None,
    include_deprecated: bool = False,
) -> list[SourceFreshness]:
    """遍历目录源，汇总各源最新数据日期与健康状态

    - 跳过 deprecated 源（默认；它们已不自动同步，必然变 stale，会假报警）
    - last_date 优先取 meta_sync_jobs.last_sync_date，缺失回退表内 MAX(date)
    """
    if today is None:
        today = date.today()
    out: list[SourceFreshness] = []

    sources = registry.get_all_sources(include_deprecated=include_deprecated)
    for src in sources:
        table_name = src.get("table_name", "")
        source_key = src.get("source_key", "")
        deprecated = bool(src.get("deprecated"))
        if deprecated and not include_deprecated:
            continue
        cron = src.get("schedule_cron", "")

        sync_job = None
        try:
            if table_name:
                sync_job = repo.get_sync_job(table_name)
            if sync_job is None and source_key != table_name:
                sync_job = repo.get_sync_job(source_key)
        except Exception:
            pass

        last_date = None
        if sync_job and sync_job.last_sync_date:
            last_date = sync_job.last_sync_date
        elif table_name and repo.table_exists(table_name):
            try:
                for _c in ("date", "日期", "时间", "trade_date", "datetime", "月份"):
                    last_date = repo.get_last_date(table_name, _c)
                    if last_date is not None:
                        break
            except Exception:
                pass

        status_label, status_color, days_text = get_stale_status(last_date, cron, today)
        days_since = None
        if last_date is not None:
            days_since = (today - last_date).days

        out.append(SourceFreshness(
            source_key=source_key,
            name=src.get("name", source_key),
            table_name=table_name,
            api_function=src.get("api_function", ""),
            cron=cron,
            last_date=last_date,
            status_label=status_label,
            status_color=status_color,
            days_text=days_text,
            days_since=days_since,
            deprecated=deprecated,
        ))
    return out
