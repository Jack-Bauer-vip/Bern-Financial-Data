"""动态表结构管理 — 自动检测并扩展新列"""

from src.db.repository import DataRepository
from src.utils.logger import logger


class DynamicSchemaManager:
    """动态 schema 管理器，在写入前自动扩展数据表列"""

    def __init__(self, repo: DataRepository):
        self.repo = repo

    def ensure_table_exists(self, table_name: str, df_columns: list[str]) -> None:
        """如果数据表不存在则自动创建

        所有列统一用 TEXT 类型，避免 DATE 类型与字符串数据的兼容问题。
        """
        if self.repo.table_exists(table_name):
            return
        from sqlalchemy import text
        cols = [f'"{c}" TEXT' for c in df_columns]
        sql = (
            f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n'
            f'    id INTEGER PRIMARY KEY AUTOINCREMENT,\n'
            f'    {",\n    ".join(cols)},\n'
            f'    created_at DATETIME DEFAULT CURRENT_TIMESTAMP\n'
            f')'
        )
        with self.repo.engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()
        logger.info("已自动创建数据表: %s (%d 列)", table_name, len(cols))

    def ensure_columns(
        self,
        table_name: str,
        df_columns: list[str],
        source_api: str = "",
    ) -> list[str]:
        """确保 DataFrame 中的所有列在数据库表中都存在

        比对 df.columns 与现有表列，自动 ALTER TABLE ADD COLUMN 添加缺失列。

        Parameters
        ----------
        table_name : str
            目标数据表名
        df_columns : list[str]
            DataFrame 的列名列表
        source_api : str
            数据来源标识（如 "akshare.stock_zh_a_hist"），用于列注册

        Returns
        -------
        list[str]
            当前表所有列的完整列表
        """
        # ★ 如果表不存在，先创建表
        self.ensure_table_exists(table_name, df_columns)

        existing = self.repo.get_all_existing_columns(table_name)
        existing_set = set(existing)

        new_cols = [c for c in df_columns if c not in existing_set]
        if not new_cols:
            return existing

        for col in new_cols:
            try:
                self.repo.add_column_to_table(table_name, col)
                self.repo.log_column_registry(table_name, col, source_api)
                logger.info("已自动扩展新列 '%s.%s'", table_name, col)
            except Exception as exc:
                logger.error("添加列 '%s.%s' 失败: %s", table_name, col, exc)

        # 重新获取完整列列表
        return self.repo.get_all_existing_columns(table_name)
