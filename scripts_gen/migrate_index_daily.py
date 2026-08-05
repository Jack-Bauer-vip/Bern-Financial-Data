# -*- coding: utf-8 -*-
"""一次性迁移：修复 index_daily 表长期停在 2014 的问题

背景：
- 表是 2014 年创建的，无 symbol 列，且带旧唯一索引 uix_index_daily_date(date)。
- 新同步按 (symbol, date) 唯一键写入；旧 (date) 唯一索引会与新插入行冲突 → 必先移除。
- 该表为单指数（sh000001 上证指数），旧行回填 symbol 后与新数据按 (symbol,date) 合并。

操作（全部可重跑，幂等）：
1. 删除过时的 uix_index_daily_date 唯一索引（schema 变更，不删数据）
2. 旧行回填 symbol='sh000001'
3. 重跑同步（全量拉取 1990→今 + 注入 symbol，幂等 upsert）

用法：python scripts_gen/migrate_index_daily.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def main() -> None:
    config = ConfigManager()
    repo = DataRepository(get_engine())

    # 1. 删除旧唯一索引（幂等）
    with repo.engine.connect() as conn:
        conn.execute(__import__("sqlalchemy").text(
            'DROP INDEX IF EXISTS "uix_index_daily_date"'))
        conn.commit()
    print("已删除旧唯一索引 uix_index_daily_date")

    # 2. 补 symbol 列（幂等，加列前回填会报 no such column）
    repo.add_column_to_table("index_daily", "symbol", "TEXT")

    # 3. 旧行回填 symbol（单指数表，默认 sh000001）
    with repo.engine.connect() as conn:
        conn.execute(__import__("sqlalchemy").text(
            'UPDATE "index_daily" SET symbol = :s '
            "WHERE symbol IS NULL OR symbol = ''"),
            {"s": "sh000001"})
        conn.commit()
    with repo.engine.connect() as conn:
        n = conn.execute(__import__("sqlalchemy").text(
            'SELECT COUNT(*) FROM "index_daily"')).scalar()
    print(f"旧行已回填 symbol，当前 {n} 行")

    # 4. 重跑同步（幂等 upsert，全量拉取 + 注入 symbol）
    engine = SyncEngine(DataFetcher(config), repo,
                        DynamicSchemaManager(repo), config)
    added = engine.run("index.daily")
    print(f"同步完成，本次写入 {added} 行")

    # 4. 验证
    with repo.engine.connect() as conn:
        n = conn.execute(__import__("sqlalchemy").text(
            'SELECT COUNT(*) FROM "index_daily"')).scalar()
        mx = conn.execute(__import__("sqlalchemy").text(
            'SELECT MAX(date) FROM "index_daily"')).scalar()
        syms = conn.execute(__import__("sqlalchemy").text(
            'SELECT DISTINCT symbol FROM "index_daily"')).fetchall()
    print(f"最终：{n} 行，最新 {mx}，symbol={[s[0] for s in syms]}")


if __name__ == "__main__":
    main()
