"""CSV 导入器 — 读取 CSV 文件为 DataFrame

兼容 utf-8-sig / gbk / utf-8 三种常见编码（中文 Excel 另存的 CSV 常为 gbk）。
"""

import pandas as pd

from src.importer.base import BaseImporter, ImportError


class CsvImporter(BaseImporter):
    """CSV 导入器"""

    extension = ".csv"
    file_filter = "CSV 文件 (*.csv)"

    # 尝试的编码顺序
    ENCODINGS = ("utf-8-sig", "gbk", "utf-8")

    def read(self, path: str) -> pd.DataFrame:
        last_err: Exception | None = None
        for enc in self.ENCODINGS:
            try:
                df = pd.read_csv(path, encoding=enc)
                # 丢弃全空列/全空行，避免脏数据
                df = df.dropna(how="all").dropna(axis=1, how="all")
                if df.empty:
                    raise ImportError("CSV 文件为空")
                return df
            except ImportError:
                raise
            except Exception as exc:
                last_err = exc
        raise ImportError(f"无法解析 CSV 文件: {last_err}")
