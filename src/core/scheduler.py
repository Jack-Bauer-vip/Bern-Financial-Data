"""定时调度引擎 — 基于 APScheduler，支持按板块独立启停"""

import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.utils.logger import logger


class DataScheduler:
    """定时调度管理器

    每个数据源分类（宏观/股票/指数/基金）可独立启停。
    从 data_catalog.yaml 读取 schedule_enabled 和 schedule_cron。
    """

    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._jobs: dict[str, str] = {}  # {category_name: job_id}
        self._job_callbacks: dict[str, callable] = {}
        # ★ 分类->任务ID映射 {category_name: [job_id, ...]}
        self._category_jobs: dict[str, list[str]] = {}

    def start(self) -> None:
        """启动调度器"""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("定时调度器已启动")

    def stop(self) -> None:
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("定时调度器已停止")

    def is_running(self) -> bool:
        return self.scheduler.running if hasattr(self.scheduler, 'running') else False

    def get_jobs(self) -> list[dict]:
        """返回所有任务信息"""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
                "pending": job.next_run_time is None,
            })
        return jobs

    # ------------------------------------------------------------------
    # 任务注册
    # ------------------------------------------------------------------

    def register_category_jobs(
        self,
        categories: list[dict],
        callback: callable,
    ) -> None:
        """从 data_catalog.yaml 的分类树注册定时任务

        Args:
            categories: data_catalog.yaml 的 categories 列表
            callback: 任务触发时调用的函数, 参数为 (source_key, category_name)
        """
        self._job_callbacks["_default"] = callback

        for cat in categories:
            self._register_category(cat, callback)

    def _register_category(
        self,
        category: dict,
        callback: callable,
        parent_key: str = "",
        top_category_name: str | None = None,
    ) -> None:
        """递归注册一个分类下的所有定时任务

        Args:
            category: 当前分类配置
            callback: 触发回调
            parent_key: 父级 source_key
            top_category_name: 顶级分类名称（用于启停分组）
        """
        name = category.get("name", "")
        source_key = category.get("source_key", "")
        children = category.get("children", [])
        schedule_cron = category.get("schedule_cron")
        schedule_enabled = category.get("schedule_enabled")

        # 记住顶级分类名
        if top_category_name is None and not children:
            top_category_name = name
        elif top_category_name is None:
            top_category_name = name

        if source_key and schedule_cron and schedule_enabled is not False:
            # 有定时表达式且已启用的节点 -> 注册任务
            job_id = self.add_job(source_key, schedule_cron, name, callback)
            if job_id and top_category_name:
                self._category_jobs.setdefault(top_category_name, []).append(job_id)
            return

        # 递归处理子节点
        if children:
            for child in children:
                self._register_category(child, callback, source_key, top_category_name)

    def add_job(
        self,
        source_key: str,
        cron_expr: str,
        name: str,
        callback: callable,
    ) -> str | None:
        """添加一个定时任务

        Args:
            source_key: 数据源标识
            cron_expr: cron 表达式 "0 22 * * 1-5"
            name: 任务显示名称
            callback: 回调函数

        Returns:
            job_id 或 None
        """
        try:
            parts = cron_expr.strip().split()
            if len(parts) != 5:
                logger.warning(f"无效 cron 表达式 [{source_key}]: {cron_expr}")
                return None

            job_id = f"sync_{source_key}"

            self.scheduler.add_job(
                func=callback,
                trigger=CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                ),
                args=[source_key, name],
                id=job_id,
                replace_existing=True,
                name=f"同步 [{name}]",
            )
            self._jobs[name] = job_id
            logger.info(f"已注册定时任务: [{name}] {cron_expr}")
            return job_id
        except Exception as e:
            logger.warning(f"注册定时任务失败 [{name}]: {e}")
            return None

    # ------------------------------------------------------------------
    # 启停控制
    # ------------------------------------------------------------------

    def enable_category(self, category_name: str) -> bool:
        """启用一个分类下所有任务"""
        job_ids = self._category_jobs.get(category_name, [])
        count = 0
        for job_id in job_ids:
            try:
                self.scheduler.resume_job(job_id)
                count += 1
            except Exception:
                pass
        logger.info(f"已启用 [{category_name}] 的 {count} 个定时任务")
        return count > 0

    def disable_category(self, category_name: str) -> bool:
        """禁用一个分类下所有任务"""
        job_ids = self._category_jobs.get(category_name, [])
        count = 0
        for job_id in job_ids:
            try:
                self.scheduler.pause_job(job_id)
                count += 1
            except Exception:
                pass
        logger.info(f"已暂停 [{category_name}] 的 {count} 个定时任务")
        return count > 0

    def set_category_enabled(self, category_name: str, enabled: bool) -> bool:
        """设置分类定时启用状态"""
        if enabled:
            return self.enable_category(category_name)
        else:
            return self.disable_category(category_name)

    def remove_category_jobs(self, category_name: str) -> int:
        """移除一个分类下所有任务"""
        job_ids = self._category_jobs.get(category_name, [])
        count = 0
        for job_id in job_ids:
            try:
                self.scheduler.remove_job(job_id)
                count += 1
            except Exception:
                pass
        if category_name in self._category_jobs:
            del self._category_jobs[category_name]
        return count
