# -*- coding: utf-8 -*-
"""跨境/QDII 指数日线 同步 CLI — 港股(恒生/国企/科技) + 全球(日经/DAX/CAC/富时/Stoxx…)

用法：
  python scripts_gen/sync_global_index.py --dry-run        # 只列同步计划，不拉取
  python scripts_gen/sync_global_index.py                  # 全部可拉取跨境指数入库（幂等）
  python scripts_gen/sync_global_index.py --codes HSI,NKY  # 只同步指定 code

原理：
- 清单来自 config/index_categories.yaml 的 global 段（分类在 sync_index_category.py 已入库）。
- 三类取数：
  · stock_hk_index_daily_sina → index.hk 源，symbol 即库内 code（HSI/HSCEI/HSTECH）
  · index_global_hist_sina    → index.global 源，新浪 symbol 用中文名（如「法国CAC40指数」）、
                                code 用拉丁码（CAC）→ 双参注入：symbol 给 API、code 写 symbol 列
  · index_global_hist_em     → 美股(SPX/NDX/DJI)，当前网络 em 被墙 → 改用 FRED 官方收盘点位补数
                               （SPX→SP500 / NDX→NASDAQ100 / DJI→DJIA），em 将来可达再全量覆盖补 OHLC
- 与 index.daily 共用 index_daily 表：?code=HSI 过滤、(symbol,date) 唯一键去重，全兼容。
- 同时写 meta_asset_info(asset_type=index) 名称 → 看板 /search 下拉可搜「恒生/日经」。
- 旁路维护，不触发 API TTL 缓存失效；/data 带 code 下沉 SQL 绕缓存，立即可见。
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

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.index_categories import parse_index_category_config
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "index_categories.yaml",
)

# 新浪全球指数：库内 code → akshare symbol（中文名，akshare index_global_sina_symbol_map 的键）
GLOBAL_SINA_SYMBOL = {
    "NKY": "日经225指数",
    "KOSPI": "首尔综合指数",
    "TWJQ": "中国台湾加权指数",
    "SENSEX": "印度孟买SENSEX指数",
    "AS51": "澳大利亚标普200指数",
    "UKX": "英国富时100指数",
    "DAX": "德国DAX 30种股价指数",
    "CAC": "法CAC40指数",          # sina 键缺「国」字（实测 2026-08-12）
    "SX5E": "欧洲Stoxx50指数",
    "GSPTSE": "加拿大S&P/TSX综合指数",
    "AS51": "澳大利亚标准普尔200指数",  # sina 键是「标准普尔」非「标普」（实测）
}

# 美股指数 → FRED 官方序列（东财 em 在本机被墙，FRED 给日频收盘点位，无 OHLC）
FRED_INDEX_SERIES = {
    "SPX": "SP500",      # 标普500（FRED 近 10 年）
    "NDX": "NASDAQ100",  # 纳斯达克100（FRED 1986 起）
    "DJI": "DJIA",       # 道琼斯工业平均（FRED 近 10 年）
}


def load_global_cfg() -> dict:
    """读 config/index_categories.yaml → global 段 {code: {name, category, sub_category, api_function}}"""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return parse_index_category_config(f.read())["global"]


def build_sync_plan(global_cfg: dict) -> list[dict]:
    """global 段 → 同步计划列表 [{code, source_key, params, fetchable}]

    - stock_hk_index_daily_sina → index.hk，symbol=code（港股）
    - index_global_hist_sina    → index.global，symbol=中文名 + code=库内码（双参注入）
    - index_global_hist_em      → code 在 FRED_INDEX_SERIES 内走 index.fred（FRED 收盘点位补数）；
                                  否则 fetchable=False（em 被墙，仅分类，数据待补）
    """
    plan: list[dict] = []
    for code, item in (global_cfg or {}).items():
        fn = item.get("api_function", "")
        if fn == "stock_hk_index_daily_sina":
            plan.append({"code": code, "source_key": "index.hk",
                         "params": {"symbol": code}, "fetchable": True})
        elif fn == "index_global_hist_sina":
            sym = GLOBAL_SINA_SYMBOL.get(code)
            if not sym:
                plan.append({"code": code, "source_key": "index.global",
                             "params": {}, "fetchable": False})
                continue
            plan.append({"code": code, "source_key": "index.global",
                         "params": {"symbol": sym, "code": code}, "fetchable": True})
        elif fn == "index_global_hist_em" and code in FRED_INDEX_SERIES:
            plan.append({"code": code, "source_key": "index.fred",
                         "params": {"series": FRED_INDEX_SERIES[code], "symbol": code},
                         "fetchable": True})
        else:
            plan.append({"code": code, "source_key": "", "params": {}, "fetchable": False})
    return plan


def sync_fred_index(repo, schema, config, code: str, series_id: str) -> int:
    """FRED 补美股指数收盘点位 → index_daily(date/close/symbol)

    FRED 序列只返回 {date, value}（收盘点位），无 OHLC → open/high/low/volume/amount
    留空(NULL)。东财 em 将来可达后重跑 em 全量覆盖即可补全 OHLC。
    增量：按该 symbol 表内 max(date) 起拉（FRED observation_start 含边界 → +1 天）。
    """
    from sqlalchemy import text

    import pandas as pd
    from src.core.fred_client import fetch_series

    schema.ensure_table_exists(
        "index_daily", ["date", "open", "high", "low", "close", "volume", "amount", "symbol"])
    start = None
    with repo.engine.connect() as conn:
        max_date = conn.execute(
            text('SELECT MAX("date") FROM "index_daily" WHERE "symbol" = :s'),
            {"s": code}).scalar()
    if max_date:
        start = (pd.to_datetime(max_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    df = fetch_series(series_id, config.fred_api_key, observation_start=start,
                      timeout=float(config.get("sync.request_timeout", 30)))
    if df.empty:
        return 0
    df = df.dropna(subset=["value"])              # FRED "." 缺失已转 NaN → 丢弃
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.rename(columns={"value": "close"})
    df["symbol"] = code
    return repo.bulk_upsert("index_daily", df[["date", "close", "symbol"]],
                            unique_columns=["symbol", "date"])


def asset_name_rows(global_cfg: dict) -> list[dict]:
    """global 段 → meta_asset_info 名称行 [{code, name, asset_type}]（全量，含 em 待补）"""
    rows = []
    for code, item in (global_cfg or {}).items():
        name = (item.get("name") or "").strip()
        if code and name:
            rows.append({"code": code, "name": name, "asset_type": "index"})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="跨境/QDII 指数日线同步")
    ap.add_argument("--dry-run", action="store_true", help="只列同步计划，不拉取")
    ap.add_argument("--codes", type=str, default=None, help="只同步指定 code，逗号分隔(HSI,NKY)")
    args = ap.parse_args()

    cfg = load_global_cfg()
    plan = build_sync_plan(cfg)
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        plan = [p for p in plan if p["code"] in want]
    if not plan:
        print("global 段为空, 退出")
        sys.exit(1)

    fetchable = [p for p in plan if p["fetchable"]]
    blocked = [p for p in plan if not p["fetchable"]]
    print(f"global 段 {len(cfg)} 个: 可拉取 {len(fetchable)} (港股 {sum(1 for p in fetchable if p['source_key']=='index.hk')}"
          f" + 全球 {sum(1 for p in fetchable if p['source_key']=='index.global')}"
          f" + FRED美股 {sum(1 for p in fetchable if p['source_key']=='index.fred')}); em 待补 {len(blocked)}")
    if blocked:
        print("  跳过(em 东财当前网络被墙, 数据待补, 分类已在): "
              + ", ".join(p["code"] for p in blocked))

    if args.dry_run:
        print("--- 同步计划 ---")
        for p in plan:
            print(f"  {p['code']:<7} -> {p['source_key']:<12} params={p['params']}")
        sys.exit(0)

    config = ConfigManager()
    repo = DataRepository(get_engine())
    schema = DynamicSchemaManager(repo)
    engine = SyncEngine(DataFetcher(config), repo, schema, config)

    # meta_asset_info 名称（全量写：含 em 待补的，/search 与 /indices 名称立即可用）
    names = asset_name_rows(cfg)
    DynamicSchemaManager(repo).ensure_table_exists(
        "meta_asset_info", ["code", "name", "asset_type"])
    repo.ensure_unique_index("meta_asset_info", ["asset_type", "code"], dedupe=True)
    import pandas as pd
    ndf = pd.DataFrame(names, columns=["code", "name", "asset_type"]).drop_duplicates(subset=["code"])
    if not ndf.empty:
        n_names = repo.bulk_upsert("meta_asset_info", ndf, unique_columns=["asset_type", "code"])
        print(f"meta_asset_info 名称写入 {n_names} 行")

    ok = fail = rows = 0
    failed_list: list[str] = []
    t0 = time.time()
    for i, p in enumerate(fetchable, 1):
        try:
            if p["source_key"] == "index.fred":
                n = sync_fred_index(repo, schema, config, p["code"], p["params"]["series"])
            else:
                n = engine.run(p["source_key"], history_years=30, params_override=p["params"])
            if n is None:
                fail += 1
                failed_list.append(p["code"])
                print(f"[{i}/{len(fetchable)}] ✗ {p['code']} 同步失败")
            else:
                ok += 1
                rows += n
                if n > 0:
                    print(f"[{i}/{len(fetchable)}] ✓ {p['code']} 写入 {n} 行")
        except Exception as exc:
            fail += 1
            failed_list.append(p["code"])
            print(f"[{i}/{len(fetchable)}] ✗ {p['code']} 异常({type(exc).__name__}: {exc})")

    el = time.time() - t0
    print("=" * 50)
    print(f"跨境指数同步完成: ok={ok} 失败={fail} 共写入 {rows} 行, 耗时 {el:.0f}s "
          f"({el / max(ok, 1):.1f}s/个)")
    if failed_list:
        print(f"失败清单({len(failed_list)}): {', '.join(failed_list)}")
    sys.exit(0 if fail == 0 else 2)


if __name__ == "__main__":
    main()
