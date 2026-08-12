# -*- coding: utf-8 -*-
"""指数日线批量同步 CLI — 初始化全市场指数到 index_daily（复用 index.daily 源）

用法：
  python scripts_gen/sync_index_batch.py --dry-run                # 只列出将同步的指数清单, 不同步
  python scripts_gen/sync_index_batch.py                          # 全量初始化(默认, 幂等)
  python scripts_gen/sync_index_batch.py --symbols sh000300,sz399001   # 只同步指定指数
  python scripts_gen/sync_index_batch.py --limit 20               # 只同步前 N 个(试跑)
  python scripts_gen/sync_index_batch.py --history-years 10       # 首次同步的历史年数(默认 5)

原理：
- akshare index_stock_info() 返回全市场指数清单(当前为裸代码 000001/399001),
  经 normalize_index_code() 归一为库内 index_daily.symbol 格式(sh/sz 前缀)。
- 逐 symbol 调 SyncEngine.run('index.daily', params_override={'symbol': ...})：
  复用增量起点(每 symbol 按实际 max(date))、3 次重试+退避、行情列规范化注入
  symbol 列、bulk_upsert 去重。已同步过的指数重跑自动增量(无新数据 → 0 行)，
  幂等；失败的指数单独跳过并计数，不中断整个批次。
- 注：index.daily 配置无 column_map，akshare stock_zh_index_daily 返回英文列
  (date/open/high/low/close/volume)，code_col 由 params_template.symbol 推导。
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

from src.core.asset_names import normalize_index_code
from src.core.data_fetcher import DataFetcher
from src.core.dynamic_schema import DynamicSchemaManager
from src.core.sync_engine import SyncEngine
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager

SOURCE_KEY = "index.daily"
TABLE_NAME = "index_daily"


def ak_index_list():
    """全市场指数清单（聚宽）→ DataFrame[index_code, display_name, ...]"""
    import akshare as ak
    return ak.index_stock_info()


def _fetch_with_retry(fn, attempts=3):
    """3 次重试 + 指数退避；最终失败返回 None（由调用方决定跳过）"""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1:
                print(f"⚠ 接口调用失败({type(exc).__name__}: {exc}), 已重试 {attempts} 次, 跳过")
                return None
            wait = 2 ** i
            print(f"⚠ 接口调用失败({type(exc).__name__}: {exc}), {wait}s 后重试...")
            time.sleep(wait)
    return None


def collect_symbols(explicit: list[str] | None, limit: int | None) -> list[str]:
    """汇总待同步的 symbol 清单（sh/sz 前缀格式）

    - explicit 给定 → 直接用（仅归一化，不拉清单接口）
    - 否则拉 akshare 全市场指数清单 → normalize_index_code 归一 → 去重保序
    """
    if explicit:
        syms = []
        for s in explicit:
            s = s.strip()
            if not s:
                continue
            if s.isdigit():
                s = normalize_index_code(s) or s  # 兼容裸代码输入
            syms.append(s)
        return syms

    raw = _fetch_with_retry(ak_index_list)
    if raw is None or getattr(raw, "empty", True):
        print("无法获取指数清单(index_stock_info), 请检查网络后重试")
        return []

    # 防御提取代码列（index_code / 代码 / code）
    code_col = None
    for cand in ("index_code", "代码", "code"):
        if cand in raw.columns:
            code_col = cand
            break
    if code_col is None:
        print(f"未识别指数清单列名(实际列: {list(raw.columns)}), 请微调")
        return []

    seen: dict[str, str] = {}
    for c in raw[code_col].dropna().astype(str):
        sym = normalize_index_code(c.strip())
        if sym:
            seen.setdefault(sym, sym)  # 保序去重
    syms = list(seen)
    if limit:
        syms = syms[:limit]
    return syms


def main() -> None:
    ap = argparse.ArgumentParser(description="指数日线批量同步（初始化全市场指数）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出将同步的指数清单, 不实际拉取")
    ap.add_argument("--symbols", type=str, default=None,
                    help="只同步指定指数, 逗号分隔(sh000300,sz399001; 兼容裸代码 000300)")
    ap.add_argument("--limit", type=int, default=None,
                    help="只同步前 N 个(试跑用)")
    ap.add_argument("--history-years", type=int, default=5,
                    help="首次同步历史年数(默认 5; 已同步的指数按实际 max(date) 增量)")
    args = ap.parse_args()

    explicit = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    symbols = collect_symbols(explicit, args.limit)
    if not symbols:
        print("待同步清单为空, 退出")
        sys.exit(1)

    print(f"待同步指数 {len(symbols)} 个")
    if args.limit:
        print(f"(--limit {args.limit} 截断)")
    for i, s in enumerate(symbols[:20], 1):
        print(f"  {i:>3}. {s}")
    if len(symbols) > 20:
        print(f"  ... 共 {len(symbols)} 个")

    if args.dry_run:
        print("--dry-run: 仅列出, 不同步")
        sys.exit(0)

    # 复用现有 index_daily 里已同步的 symbol（增量模式下会各自按 max(date) 补数）
    config = ConfigManager()
    repo = DataRepository(get_engine())
    engine = SyncEngine(DataFetcher(config), repo, DynamicSchemaManager(repo), config)

    ok = fail = rows = 0
    failed_list: list[str] = []
    t0 = time.time()
    for i, sym in enumerate(symbols, 1):
        try:
            n = engine.run(SOURCE_KEY, history_years=args.history_years,
                           params_override={"symbol": sym})
            if n is None:
                fail += 1
                failed_list.append(sym)
                print(f"[{i}/{len(symbols)}] ✗ {sym} 同步失败")
            else:
                ok += 1
                rows += n
                if n > 0:
                    print(f"[{i}/{len(symbols)}] ✓ {sym} 写入 {n} 行")
        except Exception as exc:
            fail += 1
            failed_list.append(sym)
            print(f"[{i}/{len(symbols)}] ✗ {sym} 异常({type(exc).__name__}: {exc})")

        if i % 10 == 0:
            el = time.time() - t0
            print(f"  进度 {i}/{len(symbols)}  ok={ok} fail={fail} 行数={rows}  已用 {el:.0f}s")

    el = time.time() - t0
    print("=" * 50)
    print(f"指数批量同步完成: ok={ok} 失败={fail} 共写入 {rows} 行, 耗时 {el:.0f}s "
          f"({el / max(ok, 1):.1f}s/个)")
    if failed_list:
        print(f"失败清单({len(failed_list)}): {', '.join(failed_list)}")
    sys.exit(0 if fail == 0 else 2)


if __name__ == "__main__":
    main()
