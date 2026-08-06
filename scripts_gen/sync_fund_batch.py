# -*- coding: utf-8 -*-
"""基金日线批量同步 CLI — 按交易日补全市场（替代逐个 code 同步）

用法：
  python scripts_gen/sync_fund_batch.py                          # 增量：从表内最大日期补到今天
  python scripts_gen/sync_fund_batch.py --days 10                # 强制补最近 10 个交易日（先清再拉）
  python scripts_gen/sync_fund_batch.py --start-date 2023-01-01  # 回溯补历史全市场（跳过已全市场覆盖日期）
  python scripts_gen/sync_fund_batch.py --start-date 2023-01-01 --end-date 2023-12-31

原理：tushare fund_daily(trade_date=) 一次调用返回当天全市场 ~2000 只基金，
逐个 code 同步 1000 只要 ~37min，按交易日批量只需 N 次调用（N=缺失交易日数）。
"""

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def main() -> None:
    ap = argparse.ArgumentParser(description="基金日线批量同步（按交易日补全市场）")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=0,
                     help="强制补最近 N 个交易日（先清掉最后 N 天再重拉；默认 0 = 增量到今天）")
    grp.add_argument("--start-date", type=str, default=None,
                     help="回溯起始日期 YYYY-MM-DD（拉该日起历史全市场，跳过已全市场覆盖日期）")
    ap.add_argument("--end-date", type=str, default=None,
                    help="回溯结束日期 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()

    config = ConfigManager()
    repo = DataRepository(get_engine())
    engine = SyncEngine(DataFetcher(config), repo, DynamicSchemaManager(repo), config)

    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None

    if args.days > 0:
        # 强制回补最近 N 个交易日：清掉最后 N 天数据再批量重拉（幂等，保证完整）
        from datetime import timedelta
        import sqlalchemy as sa
        max_date = repo.get_last_date("fund_etf_daily", "date") or date.today()
        cutoff = (max_date - timedelta(days=args.days * 2)).strftime("%Y-%m-%d")
        with repo.engine.connect() as conn:
            conn.execute(sa.text(
                'DELETE FROM "fund_etf_daily" WHERE "date" >= :c'), {"c": cutoff})
            conn.commit()
        print(f"已清除 {cutoff} 之后的数据，开始强制回补最近 {args.days} 个交易日")

    total = engine.run_fund_daily_batch(start_date=start_date, end_date=end_date)
    print(f"批量同步完成，本次写入 {total} 行")


if __name__ == "__main__":
    main()
