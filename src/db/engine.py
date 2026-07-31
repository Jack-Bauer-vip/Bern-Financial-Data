"""数据库引擎管理 — 支持 SQLite WAL 模式"""

import logging
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from src.utils.config import ConfigManager

logger = logging.getLogger("bern.db")


class EngineFactory:
    """SQLAlchemy 引擎工厂（单例）"""

    _instance = None
    _engine: Engine | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_engine(self) -> Engine:
        """返回全局共享的 SQLAlchemy 引擎（惰性创建）"""
        if self._engine is not None:
            return self._engine

        config = ConfigManager()
        db_path: Path = config.db_path

        # 自动创建数据目录
        db_path.parent.mkdir(parents=True, exist_ok=True)

        db_url = f"sqlite:///{db_path.as_posix()}"
        logger.info("创建数据库引擎: %s", db_url)

        self._engine = create_engine(
            db_url,
            connect_args={"timeout": 10, "check_same_thread": False},
            echo=False,
        )

        # 连接时启用 WAL 模式
        @event.listens_for(self._engine, "connect")
        def _enable_wal(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return self._engine

    def dispose(self) -> None:
        """释放引擎连接池"""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            logger.info("数据库引擎已释放")


def get_engine() -> Engine:
    """便捷函数：获取全局数据库引擎"""
    return EngineFactory().get_engine()
