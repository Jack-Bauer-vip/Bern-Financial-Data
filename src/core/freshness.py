# -*- coding: utf-8 -*-
"""数据源新鲜度 — 纯函数，供 GUI HealthDialog / API /health / CLI 脚本三方共用

从 src/gui/dialogs/health_dialog.py 拆出（health_dialog 顶部 import PySide6，
CLI 脚本不能 import 它，故纯逻辑独立成模块）。
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd

# 长任务僵死判定阈值：running 状态但心跳超过该分钟数未刷新 → 疑似僵死
HEARTBEAT_STALE_MINUTES = 10


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

# 由 infer_frequency 推断出的实际数据频率 → 预期更新间隔（天）
# 日频 2 天 / 周频 9 天 / 月频 32 天 / 季频 95 天 / 年频 370 天（含发布延迟缓冲）
_FREQ_EXPECTED_DAYS = {"D": 2, "W": 9, "M": 32, "Q": 95, "Y": 370}


def get_expected_interval_days(cron: str) -> float:
    """从 cron 表达式推算预期更新间隔（天）"""
    if not cron:
        return DEFAULT_EXPECTED_DAYS
    cron = cron.strip()
    return CRON_TO_EXPECTED_DAYS.get(cron, DEFAULT_EXPECTED_DAYS)


def infer_expected_days_from_dates(
    dates: "pd.Series",
) -> float | None:
    """从日期序列推断实际更新频率对应的预期间隔（天）

    用 transform.infer_frequency 的中位间隔法：月频 → 32 天、季频 → 95 天等。
    比纯 cron 推断更准确——FRED 月频指标的 cron 是工作日拉取，但数据本身
    月频发布，按 cron 推断会把正常月频误报为"停更"。
    无法推断（不足 2 个日期）→ None。
    """
    from src.core.transform import infer_frequency
    if dates is None or len(dates) < 2:
        return None
    freq = infer_frequency(dates)
    if freq is None:
        return None
    return _FREQ_EXPECTED_DAYS.get(freq)


def get_stale_status(
    last_date: Optional[date],
    cron: str,
    today: date = None,
    expected_days: float | None = None,
) -> tuple[str, str, str]:
    """判断数据源健康状态

    expected_days : 可选，按实际数据频率推算的预期间隔（天）；提供则优先于 cron 推断。

    Returns:
        (status_label, status_color_hex, days_since_update_str)
    """
    if today is None:
        today = date.today()

    if last_date is None:
        return ("未同步", "#888888", "从未同步")

    days_since = (today - last_date).days
    expected_days = expected_days if expected_days is not None else get_expected_interval_days(cron)
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


def is_heartbeat_stale(
    running_status: str,
    last_heartbeat: datetime | None,
    now: datetime | None = None,
) -> bool:
    """长任务僵死检测：running 状态但心跳过期（> HEARTBEAT_STALE_MINUTES）→ True

    running 且从未心跳（last_heartbeat=None）也视为疑似僵死。
    """
    if running_status != "running":
        return False
    if last_heartbeat is None:
        return True
    now = now or datetime.now()
    try:
        delta = now - last_heartbeat
    except TypeError:
        return True
    return delta.total_seconds() > HEARTBEAT_STALE_MINUTES * 60


def running_heartbeat_label(
    running_status: str,
    last_heartbeat: datetime | None,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """长任务运行状态标签：运行中(蓝) / 疑似僵死(暗红)；非 running 返回 (None, None)"""
    if running_status != "running":
        return None, None
    if is_heartbeat_stale(running_status, last_heartbeat, now):
        return "疑似僵死", "#8b0000"
    return "运行中", "#1e88e5"


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
    running_status: str = "idle"
    last_heartbeat: datetime | None = None


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
        running_status = "idle"
        last_heartbeat = None
        try:
            if table_name:
                sync_job = repo.get_sync_job(table_name)
            if sync_job is None and source_key != table_name:
                sync_job = repo.get_sync_job(source_key)
            if sync_job is not None:
                # 长任务运行状态/心跳（P1）——健康检查据此判「疑似僵死」
                running_status = getattr(sync_job, "running_status", "idle") or "idle"
                last_heartbeat = getattr(sync_job, "last_heartbeat", None)
        except Exception:
            pass

        # 实际数据频率 → 预期间隔（优先于 cron 推断；FRED 月频等 cron 是工作日
        # 但数据月频发布，按 cron 推断会误报"停更"）
        expected_days = None
        last_date = None
        if sync_job and sync_job.last_sync_date:
            last_date = sync_job.last_sync_date
        if table_name and repo.table_exists(table_name):
            try:
                cols = repo.get_all_existing_columns(table_name)
                date_col = next(
                    (c for c in cols if c.lower() in
                     ("date", "时间", "日期", "月份", "datetime", "trade_date")),
                    None)
                if date_col:
                    if last_date is None:
                        last_date = repo.get_last_date(table_name, date_col)
                    # 取最近 8 个日期推断实际频率
                    try:
                        recent = repo.query(
                            table_name, order_by=f'"{date_col}" DESC', limit=8)
                        if not recent.empty and date_col in recent.columns:
                            dates = pd.to_datetime(recent[date_col], errors="coerce")
                            dates = dates.dropna()
                            expected_days = infer_expected_days_from_dates(dates)
                    except Exception:
                        pass
            except Exception:
                pass

        status_label, status_color, days_text = get_stale_status(
            last_date, cron, today, expected_days=expected_days)
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
            running_status=running_status,
            last_heartbeat=last_heartbeat,
        ))
    return out
