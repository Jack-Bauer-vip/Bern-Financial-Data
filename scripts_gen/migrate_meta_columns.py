# -*- coding: utf-8 -*-
"""元数据表补列迁移（幂等）— P0 指标口径 + P1 心跳字段

为 meta_indicator 补 unit_type/unit_desc（存储值口径语义），
为 meta_sync_jobs 补 running_status/last_heartbeat（长任务心跳）。

回填：从 config/data_catalog.yaml 读取带 indicator 键的叶节点的 unit_type/unit_desc，
按 preferred_table 匹配现有 meta_indicator 行回填（FRED 源 → level 等）。

用法：
  python scripts_gen/migrate_meta_columns.py    # 幂等，可重复执行

实现要点：
- ALTER TABLE ADD COLUMN 前用 PRAGMA table_info 检查列存在性（幂等，重复执行安全）。
- 回填只 UPDATE 当前 unit_type 仍为默认 level 且目录中有对应 unit 的行，
  不覆盖用户后续手动调整的口径。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.fetcher_registry import FetcherRegistry
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager

# 目标列：(表, 列名, DDL 类型, 默认值/可空)
_ALTERS = [
    ("meta_indicator", "unit_type", "VARCHAR(16)", "'level'"),
    ("meta_indicator", "unit_desc", "VARCHAR(128)", "NULL"),
    ("meta_sync_jobs", "running_status", "VARCHAR(16)", "'idle'"),
    ("meta_sync_jobs", "last_heartbeat", "DATETIME", "NULL"),
]


def _existing_columns(repo: DataRepository, table: str) -> set[str]:
    """PRAGMA table_info 拿现有列名集合"""
    return set(repo.get_all_existing_columns(table))


def _add_missing_columns(repo: DataRepository) -> int:
    """幂等补列，返回新增列数"""
    added = 0
    with repo.engine.connect() as conn:
        for table, col, ddl_type, default in _ALTERS:
            if col in _existing_columns(repo, table):
                continue
            conn.exec_driver_sql(
                f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ddl_type} DEFAULT {default}')
            added += 1
        conn.commit()
    return added


def _catalog_unit_by_table() -> dict[str, dict]:
    """收集目录中所有带 indicator 键的叶节点：table_name → {unit_type, unit_desc}"""
    registry = FetcherRegistry(ConfigManager())
    out: dict[str, dict] = {}
    for src in registry.get_all_sources():
        if not src.get("indicator"):
            continue
        table = src.get("table_name")
        if not table:
            continue
        out[table] = {
            "unit_type": str(src.get("unit_type", "level")),
            "unit_desc": src.get("unit_desc") or "",
        }
    return out


def _backfill_indicator_units(repo: DataRepository) -> int:
    """按 preferred_table 匹配目录 unit 回填 meta_indicator；返回更新行数"""
    by_table = _catalog_unit_by_table()
    if not by_table:
        return 0

    updated = 0
    with repo.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            'SELECT id, indicator_key, preferred_table, unit_type, unit_desc '
            'FROM meta_indicator').mappings().all()
        for r in rows:
            src_unit = by_table.get(r["preferred_table"])
            if not src_unit:
                continue
            # 目录中标了 unit_desc 且当前为空 → 回填；已有人工口径的不覆盖
            if src_unit["unit_desc"] and not r["unit_desc"]:
                conn.exec_driver_sql(
                    'UPDATE meta_indicator SET unit_type = ?, unit_desc = ? '
                    'WHERE id = ?',
                    (src_unit["unit_type"], src_unit["unit_desc"], r["id"]))
                updated += 1
        conn.commit()
    return updated


def main() -> None:
    repo = DataRepository(get_engine())

    added = _add_missing_columns(repo)
    print(f"补列完成：新增 {added} 个列（重复执行应为 0）")

    filled = _backfill_indicator_units(repo)
    print(f"口径回填：更新 {filled} 条 meta_indicator（按获信源表匹配目录）")

    # 校验输出
    with repo.engine.connect() as conn:
        cols = set(conn.exec_driver_sql(
            "SELECT name FROM pragma_table_info('meta_indicator')").scalars().all())
        print(f"meta_indicator 现有列: {sorted(cols)}")
        n = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM meta_indicator "
            "WHERE unit_desc IS NOT NULL AND unit_desc != ''").scalar()
        print(f"已有口径说明的指标行数: {n}")


if __name__ == "__main__":
    main()
