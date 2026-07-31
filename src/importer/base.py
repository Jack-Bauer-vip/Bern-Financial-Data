"""导入基类 — 定义导入器接口和共享工具"""

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd


class ImportError(Exception):
    """导入异常"""
    pass


class BaseImporter(ABC):
    """导入器抽象基类"""

    # 文件扩展名（含点）
    extension: str = ""

    # 文件过滤描述
    file_filter: str = ""

    @abstractmethod
    def read(self, path: str) -> pd.DataFrame:
        """从文件读取数据为 DataFrame"""
        ...

    def validate(self, df: pd.DataFrame) -> Optional[str]:
        """导入前校验，返回错误信息或 None"""
        if df is None or df.empty:
            return "文件没有可导入的数据"
        if df.columns.empty:
            return "文件没有可识别的列"
        return None

    def suggested_filename(self) -> str:
        """建议的文件名（不含扩展名）"""
        return "import"
