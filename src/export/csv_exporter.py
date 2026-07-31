"""CSV 导出器 — 支持编码选择和分隔符选项"""

import pandas as pd
from src.export.base import BaseExporter, ExportError


class CsvExporter(BaseExporter):
    """CSV 格式导出器"""

    extension = ".csv"
    file_filter = "CSV 文件 (*.csv)"

    def __init__(self, df: pd.DataFrame, options: dict = None):
        super().__init__(df, options or {})
        self.encoding = self.options.get("encoding", "utf-8-sig")
        self.separator = self.options.get("separator", ",")
        self.index = self.options.get("include_index", False)

    def export(self, path: str) -> str:
        path = self.ensure_extension(path, ".csv")
        try:
            self.df.to_csv(
                path,
                index=self.index,
                encoding=self.encoding,
                sep=self.separator,
                date_format="%Y-%m-%d",
            )
            return path
        except Exception as e:
            raise ExportError(f"CSV 导出失败: {e}") from e
