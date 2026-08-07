# -*- coding: utf-8 -*-
"""数据源新鲜度检查 CLI — 列出各数据源最新日期与健康状态

用法：
  python scripts_gen/check_freshness.py                 # 全部源
  python scripts_gen/check_freshness.py --only-stale    # 只看 滞后/停更
  python scripts_gen/check_freshness.py --webhook       # 有停更且配置 env 时 POST

环境变量：
  FRESHNESS_WEBHOOK_URL  有停更时推送的 webhook（钉钉/飞书/Server酱 通用 JSON POST）
  FRESHNESS_MIN_DAYS     只看距今天数 >= 此值的源（可选）

退出码：有停更源 → 1；否则 0（便于定时任务/CI 判断）。
依赖仅 src.core / src.db，不 import PySide6（GUI 对话框不可在此引入）。
"""

import argparse
import json
import os
import sys

# Windows GBK 控制台兜底：避免 ✅/⚠️/🔴 等符号 UnicodeEncodeError 崩溃。
# 仅改 errors（保留原 encoding，中文在 GBK 下仍正常显示），无法编码的字符替换为 '?'。
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

# 确保项目根目录在 sys.path 中（从任意 cwd 运行都能 import src）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.freshness import collect_source_freshness
from src.core.fetcher_registry import FetcherRegistry
from src.db.engine import get_engine
from src.db.repository import DataRepository
from src.utils.config import ConfigManager


def _print_table(rows) -> None:
    """打印对齐表格"""
    headers = ["状态", "数据源", "最新日期", "距今天数", "更新频率"]
    widths = [len(h) for h in headers]
    lines = []
    # 状态符号映射为 ASCII（⚠/✅/🔴 不在 GBK 字符集，老式 Windows 控制台会崩）
    _symbol_map = {"✅": "[OK]", "⚠️": "[!]", "🔴": "[X]"}

    def _safe(s):
        if not isinstance(s, str):
            return s
        for _k, _v in _symbol_map.items():
            s = s.replace(_k, _v)
        return s

    for r in rows:
        freq = r.cron if r.cron else "手动"
        lines.append([
            _safe(r.status_label),
            _safe(r.name),
            r.last_date.isoformat() if r.last_date else "从未同步",
            f"{r.days_since}天" if r.days_since is not None else "-",
            freq,
        ])
    for line in lines:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join("{" + str(i) + ":<" + str(w) + "}"
                    for i, w in enumerate(widths))
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for line in lines:
        print(fmt.format(*line))


def _notify(stale) -> None:
    """有停更源时 POST 到 webhook（钉钉/飞书/Server酱 兼容）"""
    url = os.getenv("FRESHNESS_WEBHOOK_URL", "").strip()
    if not url:
        return
    items = [{
        "source_key": s.source_key,
        "name": s.name,
        "status": s.status_label,
        "last_date": s.last_date.isoformat() if s.last_date else None,
        "days_since": s.days_since,
    } for s in stale]
    payload = {
        "msgtype": "text",
        "text": {
            "content": "【金融数据中台】数据源新鲜度告警：%d 个源滞后/停更\n%s"
                       % (len(stale), json.dumps(items, ensure_ascii=False)[:2000]),
        },
    }
    try:
        import httpx
        r = httpx.post(url, json=payload, timeout=15)
        print("已推送 webhook:", r.status_code)
    except Exception as exc:
        print("webhook 推送失败:", exc)


def main() -> None:
    ap = argparse.ArgumentParser(description="数据源新鲜度检查")
    ap.add_argument("--only-stale", action="store_true", help="只列出滞后/停更源")
    ap.add_argument("--webhook", action="store_true",
                    help="有停更时 POST 到 FRESHNESS_WEBHOOK_URL（默认只打印）")
    ap.add_argument("--min-days", type=int, default=0,
                    help="只看距今天数 >= 此值的源")
    args = ap.parse_args()

    min_days = int(os.getenv("FRESHNESS_MIN_DAYS") or args.min_days or 0)

    repo = DataRepository(get_engine())
    registry = FetcherRegistry(ConfigManager())
    rows = collect_source_freshness(repo, registry)

    # health_check_ignore 源（保留 active 但静默）：不参与停更告警/退出码/webhook
    stale = [r for r in rows
             if r.status_label != "✅ 正常" and not r.health_check_ignore]
    if min_days:
        stale = [r for r in stale if (r.days_since or 0) >= min_days]

    shown = stale if args.only_stale else rows
    if not shown:
        print("所有数据源均正常 ✅")
    else:
        _print_table(shown)
        print(f"\n共 {len(rows)} 源，其中 {len(stale)} 个滞后/停更")

    if args.webhook and stale:
        _notify(stale)

    sys.exit(0 if not stale else 1)


if __name__ == "__main__":
    main()
