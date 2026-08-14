# -*- coding: utf-8 -*-
"""无 GUI 独立 API 服务 — 供网页看板 /dashboard 与下游程序只读查询。

用法：
  python scripts_gen/serve_api.py                    # 默认 host/port 取自 .env(127.0.0.1:8765)
  python scripts_gen/serve_api.py --port 9000        # 指定端口
  python scripts_gen/serve_api.py --with-scheduler   # 同时启动定时调度器(每日自动同步)

与桌面端的区别：
  - 不带 PySide6 GUI, 默认不启动调度器(不自动同步), 纯只读查询服务
  - `--with-scheduler` 时启动定时调度器：按 config/data_catalog.yaml 的
    schedule_cron 每日自动同步（宏观 cron 已有；行情 fund_etf_daily /
    index_daily 走按交易日批量，见 sync_mode 配置），供「D 全自动每日更新」
    的 Windows 计划任务场景使用。每次同步完成后写 data/sync_ready.json
    就绪标记（data_asof），/api/v1/health 一并暴露，供 A/B 读前校验新鲜度。
  - 若桌面端已在跑(8765 已被占用), 请直接关掉本服务, 用桌面端即可
  - 看板页面/静态资源免鉴权; /api/v1 数据请求仍按 .env API_TOKEN 校验

配套启动器: 根目录 start_dashboard.bat(自动探测端口, 未起则后台拉起本脚本再开浏览器)。
"""

import argparse
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows GBK 控制台兜底：避免 ✓/✗ 等符号 UnicodeEncodeError 崩溃
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

from src.api.server import FastAPIServer
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager
from src.utils.logger import logger


def _build_headless_scheduler(config, repo):
    """构造 headless 定时调度器（含按 data_catalog.yaml 注册的任务与同步回调）

    回调在后台线程执行同步（SyncEngine 信号发射无需 GUI 事件循环）；
    完成后写就绪标记（涉及表 = 该源的 table_name）。
    """
    from src.core.data_fetcher import DataFetcher
    from src.core.dynamic_schema import DynamicSchemaManager
    from src.core.fetcher_registry import FetcherRegistry
    from src.core.scheduler import DataScheduler
    from src.core.sync_engine import SyncEngine
    from src.core.sync_ready import write_sync_ready

    registry = FetcherRegistry(config)

    def _headless_sync(source_key: str, category_name: str) -> None:
        """定时触发回调（APScheduler 线程）→ 后台线程跑同步，完成后写就绪标记"""
        def _run():
            try:
                logger.info("[调度] 开始同步 %s -> %s", category_name, source_key)
                # 与 GUI 定时回调一致：每次新建独立 SyncEngine/repo（避免跨线程共享）
                fetcher = DataFetcher(ConfigManager())
                repo2 = DataRepository(get_engine())
                schema = DynamicSchemaManager(repo2)
                sync = SyncEngine(fetcher, repo2, schema, ConfigManager())
                # sync.run() 按 source 的 sync_mode 自动路由：
                #   fund_daily_batch → run_fund_daily_batch / index_daily_batch → run_index_daily_batch
                result = sync.run(source_key)
                # 更新完写就绪标记（涉及表 = 该源配置的 table_name）
                src_cfg = registry.get_source(source_key)
                tables = ([src_cfg["table_name"]]
                          if src_cfg and src_cfg.get("table_name") else None)
                write_sync_ready(repo2, tables)
                logger.info("[调度] 完成同步 %s -> %s: 写入 %s 行",
                            category_name, source_key, result)
            except Exception as exc:
                logger.error("[调度] 同步失败 %s -> %s: %s", category_name, source_key, exc)

        threading.Thread(target=_run, daemon=True).start()

    scheduler = DataScheduler()
    categories = config.catalog.get("categories", [])
    scheduler.register_category_jobs(categories, _headless_sync)
    return scheduler


def main() -> None:
    config = ConfigManager()
    parser = argparse.ArgumentParser(description="Bern 数据中台 无GUI 只读 API 服务")
    parser.add_argument("--host", default=config.api_host, help=f"监听地址(默认 {config.api_host})")
    parser.add_argument("--port", type=int, default=config.api_port, help=f"监听端口(默认 {config.api_port})")
    parser.add_argument("--with-scheduler", action="store_true",
                        help="同时启动定时调度器(按 data_catalog.yaml 的 schedule_cron 每日自动同步, 含行情批量)")
    args = parser.parse_args()

    repo = DataRepository(get_engine())
    try:
        repo.create_tables()  # 幂等; 防全新 DB 无表
    except Exception:
        pass

    # 无 --with-scheduler: scheduler=None, 纯只读查询(同步请走桌面端)
    scheduler = None
    if args.with_scheduler:
        try:
            scheduler = _build_headless_scheduler(config, repo)
            scheduler.start()
            print(f"[serve_api] 定时调度器已启动（{len(scheduler.get_jobs())} 个任务）")
        except Exception as exc:
            print(f"[serve_api] 定时调度器启动失败，继续只读服务: {exc}", file=sys.stderr)
            scheduler = None

    server = FastAPIServer(repo=repo, scheduler=scheduler)
    ok = server.start(args.host, args.port)
    if not ok:
        if scheduler is not None:
            scheduler.stop()
        print(f"[serve_api] 端口 {args.port} 启动失败(可能已被桌面端占用), 退出。", file=sys.stderr)
        sys.exit(1)

    print(f"[serve_api] 只读 API 服务已启动:")
    print(f"   看板:   http://{args.host}:{args.port}/dashboard")
    print(f"   接口:   http://{args.host}:{args.port}/api/v1/health")
    print(f"   文档:   http://{args.host}:{args.port}/docs")
    print(f"   调度器: {'运行中' if scheduler is not None else '未启动(--with-scheduler 开启)'}")
    print("[serve_api] 按 Ctrl+C 停止服务。")

    try:
        while True:
            import time
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
        if scheduler is not None:
            scheduler.stop()
        print("\n[serve_api] 已停止。")


if __name__ == "__main__":
    main()
