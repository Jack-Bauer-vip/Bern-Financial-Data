# -*- coding: utf-8 -*-
"""数据当日可用性测试 — 定时(如 15:30 / 16:00)探测各类型数据能否更新到当天

用途：摸清「各类型数据在什么时间点能拿到当天最新值」，为将来配置定时同步
时间点提供依据。每轮做：
  ① ETF基金 / 股票 / 指数 各测一个代表（增量同步 + 查该 code 最大日期）
  ② 全部启用宏观源（增量同步 + 查表最大日期）——月频/季频源在非发布日不会
     出现今天，属正常；日频/周频源（FRED 国债、SHIBOR、初请等）应能到当天。

用法：
  python scripts_gen/test_daily_availability.py             # 完整一轮
  python scripts_gen/test_daily_availability.py --no-macro   # 只测资产代表
  python scripts_gen/test_daily_availability.py --no-assets  # 只测宏观源
  python scripts_gen/test_daily_availability.py --limit 2    # 资产代表只测前 N 个

输出：按类型分组打印 源 → 最新日期 → 是否=今天。全部同步幂等，可重复跑。
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

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

# 宏观点表日期列名多样(date/日期/时间/月份/季度/TRADE_DATE/发布日期)，逐列探测
_DATE_COLS = ["date", "日期", "时间", "月份", "季度", "TRADE_DATE", "发布日期"]


def _latest_raw(repo, table: str):
    """读表内最新日期列的原始 MAX 字符串（列名自适应）"""
    if not repo.table_exists(table):
        return None
    with repo.engine.connect() as conn:
        cols = [r[1] for r in conn.execute(
            text(f'PRAGMA table_info("{table}")')).fetchall()]
        for c in _DATE_COLS:
            if c in cols:
                row = conn.execute(
                    text(f'SELECT MAX("{c}") FROM "{table}"')).fetchone()
                return row[0] if row else None
    return None


def _is_today(val) -> bool:
    """ISO/YYYYMMDD 日期列才比较；中文月份/季度串不做今日判断"""
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d == date.today()
        except ValueError:
            continue
    return False


def _probe_tushare_index(engine, repo) -> None:
    """tushare index_daily 功能探测：不写库、不要求最新数据

    用「已知交易日」(境内 index_daily 现有最大日期) 验证接口功能正常，
    另探测「今日」是否已发布(trade_date 返回 >0 行即已发布)。
    """
    print("\n[tushare 指数批量功能探测]")
    try:
        pro = engine.fetcher.tushare_pro
    except Exception as exc:
        print(f"   ✗ tushare 不可用: {type(exc).__name__}: {exc}")
        return
    if pro is None:
        print("   ✗ tushare 未配置 token")
        return
    try:
        dom_max = repo.get_last_date_where(
            "index_daily", "date", 'symbol LIKE "sh%" OR symbol LIKE "sz%"')
    except Exception:
        dom_max = None
    known = dom_max or (date.today() - timedelta(days=1))
    for label, d in (("已知交易日(功能)", known), ("今日", date.today())):
        try:
            df = pro.index_daily(trade_date=d.strftime("%Y%m%d"))
            n = 0 if df is None or df.empty else len(df)
            mark = " ✅今日已发布" if label == "今日" and n > 0 else ""
            print(f"   trade_date={d} {label}: 返回 {n} 行{mark}")
        except Exception as exc:
            print(f"   trade_date={d} {label}: 调用失败 "
                  f"{type(exc).__name__}: {str(exc)[:80]}")

# 每类资产挑一个代表：source_key + 覆盖参数 + 查最大日期的 (表, 代码列, 代码值)
ASSET_REPS = [
    ("ETF基金", "fund.etf_daily", {"ts_code": "510300.SH"}, "fund_etf_daily", "code", "510300"),
    ("股票", "stock.a_daily", {"ts_code": "600519.SH"}, "stock_daily", "symbol", "600519"),
    ("指数", "index.daily", {"symbol": "sh000001"}, "index_daily", "symbol", "sh000001"),
]


def _fmt(d):
    if d is None:
        return "无数据"
    return f"{d} {'✅今天' if _is_today(d) else ''}"


def main() -> None:
    ap = argparse.ArgumentParser(description="数据当日可用性测试")
    ap.add_argument("--no-macro", action="store_true", help="跳过宏观源")
    ap.add_argument("--no-assets", action="store_true", help="跳过资产代表")
    ap.add_argument("--no-tushare", action="store_true", help="跳过 tushare 指数批量功能探测")
    ap.add_argument("--limit", type=int, default=None, help="资产代表只测前 N 个")
    args = ap.parse_args()

    config = ConfigManager()
    repo = DataRepository(get_engine())
    engine = SyncEngine(DataFetcher(config), repo, DynamicSchemaManager(repo), config)
    today = date.today()
    t0 = time.time()

    # ① 资产代表
    if not args.no_assets:
        reps = ASSET_REPS[: args.limit] if args.limit else ASSET_REPS
        print(f"[1/3] 资产代表测试（{len(reps)} 个）…")
        for label, src_key, override, table, code_col, code_val in reps:
            try:
                n = engine.run(src_key, params_override=override)
                failed = n is None
            except Exception as exc:
                print(f"   ✗ {label}: 同步异常 {type(exc).__name__}: {exc}")
                failed = True
            try:
                d = repo.get_max_date(table, code_col, code_val)
            except Exception:
                d = None
            mark = " ⚠️同步失败" if failed else ""
            print(f"   {label:<6} [{src_key}] 最新 {_fmt(d)}{mark}")

    # ② tushare 指数批量功能探测
    if not args.no_tushare:
        _probe_tushare_index(engine, repo)

    # ③ 宏观源
    if not args.no_macro:
        sources = repo.get_all_enabled_sources()
        macro = [s for s in sources if (s.get("source_key") or "").startswith("macro.")]
        print(f"\n[3/3] 宏观源测试（启用 {len(macro)} 个）…")
        for s in macro:
            key = s.get("source_key")
            table = s.get("table_name") or key
            try:
                n = engine.run(key)
                failed = n is None
            except Exception as exc:
                print(f"   ✗ {key}: 同步异常 {type(exc).__name__}: {exc}")
                failed = True
            try:
                d = _latest_raw(repo, table)
            except Exception:
                d = None
            mark = " ⚠️同步失败" if failed else ""
            print(f"   {key:<45} 最新 {_fmt(d)}{mark}")

    print(f"\n一轮可用性测试完成，耗时 {time.time() - t0:.0f}s"
          f"（月频/季频宏观源在非发布日最新日期为上月/上季值属正常）")


if __name__ == "__main__":
    main()
