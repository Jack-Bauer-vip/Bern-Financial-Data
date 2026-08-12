# -*- coding: utf-8 -*-
"""复权因子同步 CLI — 股票/ETF 复权因子批量入库

用法：
  python scripts_gen/sync_adj_factor.py --asset-type stock             # 股票因子增量
  python scripts_gen/sync_adj_factor.py --asset-type stock --start-date 2014-01-01   # 回溯历史
  python scripts_gen/sync_adj_factor.py --asset-type fund              # ETF 因子（tushare fund_adj）
  python scripts_gen/sync_adj_factor.py --asset-type fund --fallback   # ETF 因子走 akshare 回退

说明：
- 因子存 asset_adj_factor 表（asset_type/code/date/factor），行情表 ODS 原值不动，
  查询端 /data/{table}?adj=qfq|hfq 派生复权价。
- --fallback 仅用于 tushare fund_adj 权限不足时：逐 code 用 akshare
  fund_etf_hist_em 的 hfq 序列 ÷ 不复权序列 反推因子（较慢，作降级/补数）。
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

from datetime import date

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    ap = argparse.ArgumentParser(description="复权因子同步")
    ap.add_argument("--asset-type", choices=["stock", "fund"], default="stock",
                    help="因子类型（stock=A股 / fund=ETF），默认 stock")
    ap.add_argument("--fallback", action="store_true",
                    help="走 akshare 回退路径（tushare fund_adj 权限不足时；仅 fund）")
    ap.add_argument("--codes", default="",
                    help="回退路径限定代码，逗号分隔（默认 fund_etf_daily 表内全部）")
    ap.add_argument("--start-date", type=_parse_date, default=None,
                    help="起始日期 YYYY-MM-DD（默认增量：该类型因子最大日期+1，首跑近 1 年）")
    ap.add_argument("--end-date", type=_parse_date, default=None,
                    help="结束日期 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    repo = DataRepository(get_engine())
    config = ConfigManager()
    engine = SyncEngine(DataFetcher(config), repo, DynamicSchemaManager(repo), config)

    if args.fallback:
        if args.asset_type != "fund":
            ap.error("--fallback 仅支持 --asset-type fund（akshare 仅基金复权路径）")
        codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
        total = engine.sync_adj_factor_fallback(
            codes=codes, asset_type="fund",
            start_date=args.start_date, end_date=args.end_date)
    else:
        total = engine.run_adj_factor_batch(
            args.asset_type, start_date=args.start_date, end_date=args.end_date)

    print(f"复权因子同步完成，共写入 {total} 行")
    sys.exit(0)


if __name__ == "__main__":
    main()
