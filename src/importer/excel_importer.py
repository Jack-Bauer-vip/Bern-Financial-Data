"""Excel 导入器 — 读取 xlsx 文件为 DataFrame（openpyxl 引擎）"""

import pandas as pd

from src.importer.base import BaseImporter, ImportError


class ExcelImporter(BaseImporter):
    """Excel (xlsx) 导入器"""

    extension = ".xlsx"
    file_filter = "Excel 文件 (*.xlsx *.xls)"

    def read(self, path: str) -> pd.DataFrame:
        try:
            df = pd.read_excel(path, engine="openpyxl")
            df = df.dropna(how="all").dropna(axis=1, how="all")
            if df.empty:
                raise ImportError("Excel 文件为空")
            return df
        except ImportError:
            raise
        except Exception as exc:
            raise ImportError(f"无法解析 Excel 文件: {exc}")
