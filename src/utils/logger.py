"""日志系统 — 同时输出到 GUI 和文件"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


class LogEmitter(logging.Handler):
    """日志发射器：将日志记录通过回调发给 GUI"""

    def __init__(self):
        super().__init__()
        self.callback = None

    def set_callback(self, callback):
        self.callback = callback

    def emit(self, record):
        if self.callback:
            msg = self.format(record)
            level = record.levelname
            self.callback(level, msg)


# 全局单例
_log_emitter = LogEmitter()


class LoggerFactory:
    """日志工厂 — 提供统一的日志实例"""

    _loggers = {}

    @classmethod
    def get_logger(cls, name: str = "bern") -> logging.Logger:
        if name in cls._loggers:
            return cls._loggers[name]

        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()

        # 控制台输出
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s - %(message)s",
            datefmt="%H:%M:%S"
        ))
        logger.addHandler(console)

        # 文件输出（按天轮转）
        log_dir = Path.cwd() / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "bern.log",
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(name)s] %(message)s"
        ))
        logger.addHandler(file_handler)

        # GUI 发射器
        _log_emitter.setLevel(logging.DEBUG)
        _log_emitter.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(_log_emitter)

        cls._loggers[name] = logger
        return logger

    @classmethod
    def set_gui_callback(cls, callback):
        """设置 GUI 日志回调"""
        _log_emitter.set_callback(callback)


logger = LoggerFactory.get_logger("bern")
