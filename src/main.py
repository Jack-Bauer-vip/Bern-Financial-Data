#!/usr/bin/env python3
"""Bern_Financial_Data — 桌面金融数据中台 入口"""

import sys
import os

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.logger import logger, LoggerFactory
from src.utils.config import ConfigManager


def main():
    config = ConfigManager()

    # 初始化日志系统
    LoggerFactory.get_logger("bern")

    logger.info("=" * 50)
    logger.info("Bern_Financial_Data 启动中...")
    logger.info(f"数据库路径: {config.db_path}")
    logger.info(f"数据目录: {config.root_dir / 'data'}")

    # 延迟导入 GUI（确保日志先初始化）
    from src.db.engine import EngineFactory, get_engine
    from src.db.repository import DataRepository
    from src.core.fetcher_registry import FetcherRegistry
    from src.core.data_fetcher import DataFetcher
    from src.core.dynamic_schema import DynamicSchemaManager
    from src.core.sync_engine import SyncEngine
    from src.gui.app import create_app
    from src.gui.main_window import MainWindow
    from src.gui.log_widget import LogWidget
    from src.gui.init_wizard import InitWizard

    # 初始化数据库
    engine = get_engine()
    repo = DataRepository(engine)
    try:
        repo.create_tables()
        logger.info("数据库表结构检查完成")
    except Exception as e:
        logger.warning(f"建表警告（可能已存在）: {e}")

    # 创建应用
    app = create_app()

    # 给日志系统设置 GUI 回调
    def gui_log_callback(level: str, message: str):
        """将日志路由到 MainWindow 的 LogWidget"""
        # 在 MainWindow 初始化后设置
        pass

    # 检查是否需要首次初始化向导
    db_path = config.db_path
    is_first_run = not db_path.exists() or db_path.stat().st_size == 0

    # 创建主窗口
    window = MainWindow()

    # 注册 GUI 日志回调
    LoggerFactory.set_gui_callback(window.log_widget.write)

    # 如果是首次运行，弹出初始化向导
    if is_first_run:
        logger.info("检测到首次运行，弹出初始化向导")
        from src.core.fetcher_registry import FetcherRegistry
        registry = FetcherRegistry(config)

        wizard = InitWizard(registry, config, window)
        if wizard.exec() == InitWizard.DialogCode.Accepted:
            modules = wizard.getSelectedModules()
            years = wizard.getHistoryYears()
            window.log_widget.write("SUCCESS",
                f"初始化向导完成，将在后台下载 {len(modules)} 个模块")
            # 用户已选择模块，等待手动点击同步
    else:
        # 非首次运行，显示数据库统计
        try:
            from src.db.models import SyncJob
            from sqlalchemy import select, func
            with engine.connect() as conn:
                count = conn.execute(select(func.count(SyncJob.id))
                                     .where(SyncJob.status == "completed")).scalar()
                logger.info(f"已有 {count} 个数据源完成过同步")
        except Exception:
            pass

    # 显示主窗口
    window.show()
    logger.info("Bern_Financial_Data 启动完成")

    # 进入事件循环
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
