# -*- coding: utf-8 -*-
"""指数日线快速增量同步 CLI — tushare 按交易日批量 + akshare 逐只兜底

为什么快：境内指数逐个 code 走 akshare stock_zh_index_daily 每次全量回传
历史(~8000 行)只为补 1 天，732 只串行 ~37 分钟。tushare index_daily
(trade_date=) 一次调用返回当天全市场(映射后 ~652 行入库)。本脚本：
  ① engine.run_index_daily_batch()  tushare 按交易日批量（补 N 天 N 次调用）
  ② akshare 逐只兜底              tushare 未覆盖的境内指数(sz399xxx 等)
                                   补到批量达到的前沿日期
补 1 天从 ~37 分钟 → ~5 分钟。

用法：
  python scripts_gen/sync_index_quick.py                    # 增量：境内最大日期+1 → 今天
  python scripts_gen/sync_index_quick.py --start-date 2026-08-10   # 回溯补历史
  python scripts_gen/sync_index_quick.py --end-date 2026-08-12     # 指定终点
  python scripts_gen/sync_index_quick.py --no-fallback      # 只跑 tushare 批量(不做 akshare 兜底)
  python scripts_gen/sync_index_quick.py --dry-run          # 只预览将做什么, 不同步
  python scripts_gen/sync_index_quick.py --limit 5          # 兜底只跑前 N 只(试跑)
注：跨境指数(HSI/NKY/SPX 等)有独立管道 sync_global_index(sina+FRED)，本脚本不处理。
"""

import argparse
import os
import sys
import time
from datetime import date, timedelta

# Windows GBK 控制台兜底
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager

TABLE_NAME = "index_daily"
DOM_WHERE = 'symbol LIKE "sh%" OR symbol LIKE "sz%"'

# 兜底只看「近 N 天仍活跃」的指数：数据停在数月/数年前的视为已停更/退市
# (退市/改代码的指数 akshare 也不会有新数据)，跳过避免每次无谓重试。
FALLBACK_LOOKBACK_DAYS = 30


def _compute_stale(
    symbol_max: dict[str, date],
    achieved: date | None,
    lookback_days: int = FALLBACK_LOOKBACK_DAYS,
) -> tuple[list[str], int]:
    """批量后落后于前沿、且近期仍活跃的 symbol 列表（akshare 兜底目标）

    返回 (stale, dead)：stale 需兜底；dead 停更超 lookback_days 直接跳过。
    规则：m < achieved 且 (achieved - m).days <= lookback_days → 兜底；
         m < achieved 但落后更久 → 死代码，跳过。
    """
    stale: list[str] = []
    dead = 0
    for s, m in symbol_max.items():
        if (m or date.min) >= (achieved or date.max):
            continue
        if m and achieved and (achieved - m).days > lookback_days:
            dead += 1
            continue
        stale.append(s)
    return stale, dead


def _per_symbol_max(repo) -> dict[str, date]:
    """境内指数每 symbol 的最大日期（symbol → date），供兜底判定用"""
    out: dict[str, date] = {}
    if not repo.table_exists(TABLE_NAME):
        return out
    with repo.engine.connect() as conn:
        rows = conn.execute(text(
            f'SELECT symbol, MAX(date) FROM "{TABLE_NAME}" WHERE {DOM_WHERE} '
            f'GROUP BY symbol'))
        for sym, d in rows:
            try:
                out[sym] = date.fromisoformat(d)
            except (ValueError, TypeError):
                pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="指数日线快速增量同步（tushare 批量 + akshare 兜底）")
    ap.add_argument("--start-date", type=str, default=None, help="回溯起始日期 YYYY-MM-DD")
    ap.add_argument("--end-date", type=str, default=None, help="回溯结束日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--no-fallback", action="store_true",
                    help="只跑 tushare 按日批量，不做 akshare 逐只兜底")
    ap.add_argument("--dry-run", action="store_true", help="只预览将做什么，不同步")
    ap.add_argument("--limit", type=int, default=None, help="兜底只跑前 N 只（试跑用）")
    args = ap.parse_args()

    config = ConfigManager()
    repo = DataRepository(get_engine())
    engine = SyncEngine(DataFetcher(config), repo, DynamicSchemaManager(repo), config)

    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None

    before = _per_symbol_max(repo)
    dom_max = max(before.values()) if before else None
    print(f"境内指数 {len(before)} 只，当前最大日期 {dom_max}")

    # 预览
    if args.dry_run:
        start = start_date or (dom_max + timedelta(days=1) if dom_max else None)
        print(f"--dry-run: 将从 {start or '(历史起)'} 拉至 {end_date or date.today()}；"
              f"兜底 tushare 未覆盖的境内指数")
        sys.exit(0)

    # ① tushare 按交易日批量
    t0 = time.time()
    print("\n[1/2] tushare 按交易日批量 …")
    rows = engine.run_index_daily_batch(start_date=start_date, end_date=end_date)
    print(f"      tushare 批量写入 {rows} 行（耗时 {time.time() - t0:.0f}s）")

    # ② akshare 逐只兜底：tushare 未覆盖、落后于批量前沿日期的境内指数
    if args.no_fallback:
        print("      (--no-fallback 已跳过 akshare 兜底)")
    else:
        after = _per_symbol_max(repo)
        achieved = max(after.values()) if after else None
        # 批量后仍落后于前沿（tushare 未覆盖）→ 需 akshare 兜底补齐；
        # 数据停更超 FALLBACK_LOOKBACK_DAYS 的停更/退市指数跳过不重试
        stale, dead = _compute_stale(after, achieved)
        if args.limit:
            stale = stale[:args.limit]
        print(f"[2/2] akshare 兜底 {len(stale)} 只（tushare 未覆盖，前沿 {achieved}；"
              f"另 {dead} 只停更超 {FALLBACK_LOOKBACK_DAYS} 天已跳过）…")
        ok = fail = fb_rows = 0
        t1 = time.time()
        for i, sym in enumerate(stale, 1):
            try:
                n = engine.run("index.daily", params_override={"symbol": sym})
                if n is None:
                    fail += 1
                    print(f"      [{i}/{len(stale)}] ✗ {sym} 同步失败")
                else:
                    ok += 1
                    fb_rows += n
            except Exception as exc:
                fail += 1
                print(f"      [{i}/{len(stale)}] ✗ {sym} 异常({type(exc).__name__}: {exc})")
        print(f"      akshare 兜底完成: ok={ok} 失败={fail} 写入 {fb_rows} 行"
              f"（耗时 {time.time() - t1:.0f}s）")

    print("\n" + "=" * 50)
    dom_max_after = max(_per_symbol_max(repo).values()) if _per_symbol_max(repo) else None
    print(f"指数快速同步完成，境内指数前沿 {dom_max_after}，总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
