"""导出基类 — 定义导出器接口和共享工具"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import pandas as pd


class ExportError(Exception):
    """导出异常"""
    pass


class BaseExporter(ABC):
    """导出器抽象基类"""

    # 文件扩展名（含点）
    extension: str = ""

    # 文件过滤描述
    file_filter: str = ""

    def __init__(self, df: pd.DataFrame, options: dict = None):
        self.df = df.copy()
        self.options = options or {}

    @abstractmethod
    def export(self, path: str) -> str:
        """导出数据到文件，返回文件路径"""
        ...

    def validate(self) -> Optional[str]:
        """导出前校验，返回错误信息或 None"""
        if self.df.empty:
            return "没有可导出的数据"
        return None

    @property
    def suggested_filename(self) -> str:
        """建议的文件名（不含扩展名）"""
        return "export"

    @staticmethod
    def ensure_extension(path: str, ext: str) -> str:
        """确保路径有正确的扩展名"""
        if not path.lower().endswith(ext.lower()):
            path += ext
        return path
