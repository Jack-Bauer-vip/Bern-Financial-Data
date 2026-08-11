# -*- coding: utf-8 -*-
"""FRED 源全量增量同步 CLI — 遍历全部 api_source='fred' 源做增量补数

用法：
  python scripts_gen/sync_fred_all.py              # 增量同步全部 FRED 源（真增量只拉缺失区间）
  python scripts_gen/sync_fred_all.py --verify     # 只查各 FRED 表当前最后日期，不同步
  python scripts_gen/sync_fred_all.py --key UNRATE # 只同步指定序列（api_function 匹配，支持逗号分隔）

用途：月频指标官方约每月中旬发布上月末值（如 CPI 7 月值 8 月中发布），
跑一遍即可自动补入。真增量只拉缺失区间，已最新的源秒回 0 行。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.fetcher_registry import FetcherRegistry
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def _fred_sources(reg: FetcherRegistry, keys_filter: set[str] | None = None) -> list[dict]:
    """取全部可同步（非 deprecated）的 FRED 源，可按 api_function 过滤"""
    sources = [
        s for s in reg.get_all_enabled_sources()
        if s.get("api_source") == "fred"
    ]
    if keys_filter:
        sources = [s for s in sources if s.get("api_function") in keys_filter]
    return sorted(sources, key=lambda s: s.get("source_key", ""))


def _max_dates(repo: DataRepository, tables: list[str]) -> dict[str, object]:
    """查每个 FRED 表的当前 max(date)"""
    result: dict[str, object] = {}
    with repo.engine.connect() as conn:
        for t in tables:
            try:
                row = conn.execute(sa.text(f'SELECT MAX("date") FROM "{t}"')).fetchone()
                result[t] = row[0] if row else None
            except Exception as exc:  # noqa: BLE001
                result[t] = f"ERR {exc}"
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="FRED 源全量增量同步")
    ap.add_argument("--verify", action="store_true", help="只查当前最后日期，不做同步")
    ap.add_argument("--key", type=str, default=None,
                    help="只同步指定 FRED 序列（api_function，逗号分隔，如 UNRATE,PAYEMS）")
    args = ap.parse_args()

    config = ConfigManager()
    repo = DataRepository(get_engine())
    reg = FetcherRegistry(config)
    engine = SyncEngine(DataFetcher(config), repo, DynamicSchemaManager(repo), config)

    keys_filter = None
    if args.key:
        keys_filter = {k.strip().upper() for k in args.key.split(",") if k.strip()}

    sources = _fred_sources(reg, keys_filter)
    if not sources:
        print("没有匹配的 FRED 源")
        return

    tables = sorted({s.get("table_name") for s in sources})
    before = _max_dates(repo, tables)

    if args.verify:
        print("=== FRED 表当前最后日期（未同步）===")
        for s in sources:
            tbl = s.get("table_name")
            print(f"{s.get('api_function'):12s} {s.get('source_key'):30s} max={before.get(tbl)}")
        return

    print(f"=== 开始增量同步 {len(sources)} 个 FRED 源 ===")
    summary: list[tuple[str, str, int | None]] = []
    for i, s in enumerate(sources, 1):
        key = s.get("source_key")
        func = s.get("api_function")
        print(f"[{i}/{len(sources)}] {func} ({key}) ...", flush=True)
        added = engine.run(key)
        summary.append((key, func, added))
        # 每次同步成功会自动清 TTL 缓存；失败（None）不中断后续源
        if added is None:
            print(f"    ✗ 失败")
        elif added == 0:
            print(f"    - 无新数据（已最新）")
        else:
            print(f"    ✓ 新增 {added} 行")

    after = _max_dates(repo, tables)

    print("\n=== 同步前后 max(date) 对比 ===")
    changed = 0
    for s in sources:
        tbl = s.get("table_name")
        b, a = before.get(tbl), after.get(tbl)
        mark = "" if b == a else " ← 有更新"
        if b != a:
            changed += 1
        print(f"{s.get('api_function'):12s} {tbl:34s} {b} → {a}{mark}")

    fails = [x for x in summary if x[2] is None]
    print(f"\n完成：{len(sources) - len(fails)}/{len(sources)} 个源同步成功，{len(fails)} 个失败，{changed} 个表有数据更新")
    if fails:
        for key, func, _ in fails:
            print(f"  失败源: {func} ({key})")


if __name__ == "__main__":
    main()
