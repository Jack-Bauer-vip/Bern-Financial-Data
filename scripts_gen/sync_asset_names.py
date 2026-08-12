# -*- coding: utf-8 -*-
"""资产代码→名称 同步 CLI — 供看板代码搜索下拉（/api/v1/search 名称补全）

用法：
  python scripts_gen/sync_asset_names.py --asset-type all      # 三类全部（默认）
  python scripts_gen/sync_asset_names.py --asset-type fund
  python scripts_gen/sync_asset_names.py --asset-type stock
  python scripts_gen/sync_asset_names.py --asset-type index

说明：
- 把「表内实际存在的 code」与 akshare 名称源 join，写 meta_asset_info(code/name/asset_type)。
  只存有数据的 code → 搜到的必有数据；表小、不膨胀、规避封基/LOF 名称污染。
- 名称源（免费无 token）：fund→ak.fund_name_em()、stock→ak.stock_info_a_code_name()、
  index→ak.index_stock_info()（指数代码归一为 sh000001/sz399001，兼容带后缀与裸代码两种格式）。
- 接口调用 3 次重试 + 指数退避；单类型最终失败则跳过，不影响 --asset-type all。
- 注意：本脚本是「旁路」维护，不触发 API 的 TTL 缓存失效，改名后搜索名称最长滞后 5 分钟。
"""

import argparse
import os
import sys
import time

# Windows GBK 控制台兜底
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.asset_names import (
    normalize_index_code,
    pick_fund_name_columns,
    pick_index_name_columns,
    pick_stock_name_columns,
)
from src.core.dynamic_schema import DynamicSchemaManager
from src.db.engine import get_engine
from src.db.repository import DataRepository

# 资产类型 → 表 / 代码列（index/stock 用 symbol 列）
TABLE_FOR_ASSET = {"fund": "fund_etf_daily", "stock": "stock_daily", "index": "index_daily"}
CODE_COL_FOR_ASSET = {"fund": "code", "stock": "symbol", "index": "symbol"}


# --- akshare 名称源（模块级函数，便于测试 monkeypatch）----------------------------


def ak_fund_names():
    """全市场基金（含封基/LOF）→ DataFrame[基金代码, 基金简称, ...]"""
    import akshare as ak
    return ak.fund_name_em()


def ak_stock_names():
    """A股全市场 → DataFrame[code, name]"""
    import akshare as ak
    return ak.stock_info_a_code_name()


def ak_index_names():
    """指数清单（聚宽）→ DataFrame[index_code(000001.XSHG), display_name, ...]"""
    import akshare as ak
    return ak.index_stock_info()


def _fetch_with_retry(fn, attempts=3):
    """3 次重试 + 指数退避；最终失败返回 None（由调用方决定跳过，不让 all 崩溃）"""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1:
                print(f"  ⚠ 接口调用失败({type(exc).__name__}: {exc}), "
                      f"已重试 {attempts} 次, 跳过该类型")
                return None
            wait = 2 ** i
            print(f"  ⚠ 接口调用失败({type(exc).__name__}: {exc}), {wait}s 后重试...")
            time.sleep(wait)
    return None


def sync_asset_type(repo, asset_type):
    """同步一个资产类型的 code→名称；返回写入行数"""
    table = TABLE_FOR_ASSET[asset_type]
    code_col = CODE_COL_FOR_ASSET[asset_type]

    # 表内实际存在的 code（保证「搜索到的必有数据」）
    in_db = set(repo.get_distinct_values(table, code_col, limit=100_000))
    if not in_db:
        print(f"[{asset_type}] {table} 无在库代码(表不存在或尚未同步), 跳过")
        return 0

    if asset_type == "fund":
        raw, pick = _fetch_with_retry(ak_fund_names), pick_fund_name_columns
    elif asset_type == "stock":
        raw, pick = _fetch_with_retry(ak_stock_names), pick_stock_name_columns
    else:
        raw, pick = _fetch_with_retry(ak_index_names), pick_index_name_columns

    if raw is None or getattr(raw, "empty", True):
        print(f"[{asset_type}] 名称源为空或不可用, 跳过")
        return 0

    cc, nc = pick(list(raw.columns))
    if not cc or not nc:
        print(f"[{asset_type}] 名称表列名未识别(实际列: {list(raw.columns)}); "
              f"跳过, 请按实际列名微调 src/core/asset_names.py 的 pick_*_name_columns")
        return 0

    df = raw[[cc, nc]].copy()
    df.columns = ["code", "name"]
    df = df.dropna(subset=["code", "name"])
    df["code"] = df["code"].astype(str).str.strip()
    df["name"] = df["name"].astype(str).str.strip()
    if asset_type == "index":
        # 兼容带后缀(聚宽)与裸代码(当前版本)两种格式
        df["code"] = df["code"].map(normalize_index_code)
        df = df.dropna(subset=["code"])
    df = df.drop_duplicates(subset=["code"])

    # 只保留在库 code（避免把库外代码也写进搜索索引）
    df = df[df["code"].isin(in_db)]
    if df.empty:
        print(f"[{asset_type}] 名称数据与在库代码无交集, 写入 0 行")
        return 0

    df["asset_type"] = asset_type
    n = repo.bulk_upsert(
        "meta_asset_info", df[["code", "name", "asset_type"]],
        unique_columns=["asset_type", "code"],
    )
    print(f"[{asset_type}] 写入 {n} 行 (在库 {len(in_db)} 个代码, 名称匹配 {len(df)})")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="资产代码→名称 同步（看板搜索用）")
    ap.add_argument("--asset-type", choices=["fund", "stock", "index", "all"],
                    default="all", help="资产类型（默认 all）")
    args = ap.parse_args()

    repo = DataRepository(get_engine())
    # 确保 meta_asset_info 表存在 + 唯一索引（幂等；meta_ 前缀自动不进 /data/tables 白名单）
    DynamicSchemaManager(repo).ensure_table_exists(
        "meta_asset_info", ["code", "name", "asset_type"])
    repo.ensure_unique_index("meta_asset_info", ["asset_type", "code"], dedupe=True)

    types = ["fund", "stock", "index"] if args.asset_type == "all" else [args.asset_type]
    total = 0
    for t in types:
        try:
            total += sync_asset_type(repo, t)
        except Exception as exc:
            print(f"[{t}] 同步异常: {type(exc).__name__}: {exc}, 跳过")
    print(f"资产名称同步完成, 共写入 {total} 行")
    sys.exit(0)


if __name__ == "__main__":
    main()
