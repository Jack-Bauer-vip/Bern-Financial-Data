# -*- coding: utf-8 -*-
"""指数分类 同步 CLI — 生成 meta_index_category 表（理杏仁式：宽基/行业/主题/风格/策略/债券/跨境/其他）

用法：
  python scripts_gen/sync_index_category.py           # 全量重建（幂等）
  python scripts_gen/sync_index_category.py --dry-run # 只打印各分类计数，不写库

说明：
- 输入：① meta_asset_info(asset_type=index) 的境内 732 指数；② config/index_categories.yaml。
- 分类逻辑（纯函数）在 src/core/index_categories.py：手动精修 manual 覆盖 → 自动分类
  （宽基白名单 + 名称关键词启发式）→ 其余归「其他」；跨境 global 清单合并进表。
- 旁路维护，不触发 API TTL 缓存失效；跑完 /indices 最长滞后 5 分钟（同 sync_asset_names）。
"""

import argparse
import os
import sys

# Windows GBK 控制台兜底
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.dynamic_schema import DynamicSchemaManager
from src.core.index_categories import (
    DEFAULT_CATEGORIES,
    build_category_rows,
    parse_index_category_config,
)
from src.db.engine import get_engine
from src.db.repository import DataRepository

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "index_categories.yaml",
)


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return parse_index_category_config(f.read())


def main() -> None:
    ap = argparse.ArgumentParser(description="指数分类生成（meta_index_category）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写库")
    args = ap.parse_args()

    repo = DataRepository(get_engine())
    config = load_config()

    # 境内指数 code→name（来自 meta_asset_info；表不存在/未同步 → 空，跨境仍可写入）
    codes_names = repo.get_asset_names("index")
    print(f"境内指数(meta_asset_info) {len(codes_names)} 个; 跨境策划 {len(config['global'])} 个")

    rows = build_category_rows(codes_names, config)
    if not rows:
        print("无任何分类行, 退出")
        sys.exit(1)

    # 统计
    order = config.get("categories") or DEFAULT_CATEGORIES
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    print("--- 分类计数 ---")
    for cat in order:
        if cat in counts:
            print(f"  {cat:>4}  {counts[cat]:>4}")
    for cat, c in sorted(counts.items()):
        if cat not in order:
            print(f"  {cat:>4}  {c:>4}  (未在 categories 中)")

    if args.dry_run:
        print(f"[dry-run] 共 {len(rows)} 行, 未写库")
        return

    # 建表 + 唯一索引 + 幂等 upsert（code 为键，名称/分类变化走 ON CONFLICT 更新）
    cols = ["code", "name", "category", "sub_category", "source"]
    DynamicSchemaManager(repo).ensure_table_exists("meta_index_category", cols)
    repo.ensure_unique_index("meta_index_category", ["code"], dedupe=True)

    import pandas as pd
    df = pd.DataFrame(rows, columns=cols)
    df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
    n = repo.bulk_upsert("meta_index_category", df, unique_columns=["code"])
    print(f"meta_index_category 写入 {n} 行 (共 {len(df)} 条; 差异为更新旧行)")
    sys.exit(0)


if __name__ == "__main__":
    main()
