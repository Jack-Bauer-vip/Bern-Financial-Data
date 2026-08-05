# -*- coding: utf-8 -*-
"""为 catalog 数据表创建普通索引（date [+ code/symbol]），加速 /data 通用查询

用法：
  python scripts_gen/ensure_indexes.py            # 全部 catalog 数据表
  python scripts_gen/ensure_indexes.py --table macro_fred_cpi   # 单表

幂等：已存在的索引自动跳过。数据表为 TEXT 列，普通索引即可加速范围过滤。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.fetcher_registry import FetcherRegistry
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def _index_columns_for(source_cfg: dict, repo: DataRepository, table: str) -> list[str]:
    """推断建索引列：date 必建；有 code/symbol 列的行情表加复合索引"""
    cols = repo.get_all_existing_columns(table)
    date_col = next((c for c in cols if c.lower() in
                     ("date", "时间", "日期", "月份", "datetime", "trade_date")), None)
    if not date_col:
        return []
    idx = [date_col]
    for c in ("code", "symbol"):
        if c in cols:
            idx.append(c)
            break
    return idx


def main() -> None:
    ap = argparse.ArgumentParser(description="为 catalog 数据表建普通索引")
    ap.add_argument("--table", default=None, help="只处理指定表（默认全部）")
    args = ap.parse_args()

    repo = DataRepository(get_engine())
    registry = FetcherRegistry(ConfigManager())

    created = 0
    for src in registry.get_all_sources():
        table = src.get("table_name")
        if not table:
            continue
        if args.table and table != args.table:
            continue
        cols = _index_columns_for(src, repo, table)
        if not cols:
            continue
        if repo.ensure_index(table, cols):
            created += 1
    print(f"完成：创建/确认 {created} 个普通索引")


if __name__ == "__main__":
    main()
