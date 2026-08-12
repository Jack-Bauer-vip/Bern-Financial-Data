# -*- coding: utf-8 -*-
"""看板前端纯函数测试 — 用 node 直接 eval stats.js 测 computePeriodReturns。

不依赖浏览器; 无 node 环境则整体 skip。覆盖:
- 数据不足 → 全 null(单行)
- 基期 close=0 → 对应项 null
- 最新 close < 2 → 全 null
- 跨年 ytd 边界: 最新 2026-01-15 时基期取 2025-12-31(而非 2025-01-01)
- 新上市标的: 无前一年数据 → 回退当年首个交易日
- 1 年窗口: 日历日 365 天截断
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATS_JS = Path(__file__).resolve().parent.parent / "src" / "api" / "web" / "stats.js"

_NODE_SCRIPT = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8')
  .replace('const Stats = ', 'var Stats = ')
  .replace(/'use strict';/g, '');
eval(src);

let failures = 0;
function check(cond, msg) {
  if (!cond) { console.error('FAIL: ' + msg); failures++; }
  else console.log('ok: ' + msg);
}
function close(a, b, eps) {
  if (a === null && b === null) return true;
  if (a === null || b === null) return false;
  return Math.abs(a - b) < (eps || 1e-6);
}
function assertItem(item, pct, baseDate, msg) {
  check(item && close(item.pct, pct), msg + ' pct=' + (item && item.pct));
  if (baseDate !== undefined) check(item && item.baseDate === baseDate, msg + ' baseDate');
}
const mk = (y, m, d, c) => ({ date: y + '-' + m + '-' + d, close: c });

// 1. 数据不足: 单行 → 全 null
let pr = Stats.computePeriodReturns([mk(2026, 1, 15, 3000)], 'date', 'close');
check(pr.today === null && pr.d5 === null && pr.d20 === null && pr.d60 === null
  && pr.ytd === null && pr.y1 === null, '单行数据不足全 null');

// 2. 基期 close=0 → 对应项 null(today null, 其余正常)
pr = Stats.computePeriodReturns([mk(2026,1,15,3000), mk(2026,1,14,0), mk(2025,12,31,2900)], 'date', 'close');
check(pr.today === null, '基期 close=0 → today null');
assertItem(pr.ytd, (3000-2900)/2900*100, '2025-12-31', '基期 0 不影响 ytd');

// 3. 最新 close < 2 → 全 null
pr = Stats.computePeriodReturns([{date:'2026-01-15',close:1.5},{date:'2026-01-14',close:1.4}], 'date', 'close');
check(pr.today === null && pr.ytd === null && pr.y1 === null, '最新 close<2 → 全 null');

// 4. 跨年 ytd 边界: 最新 2026-01-15, 基期必须 2025-12-31(前一年末)
const rows = [
  mk(2026,1,15,3000), mk(2026,1,14,2990), mk(2026,1,13,2985), mk(2026,1,12,2980),
  mk(2026,1,9,2975), mk(2026,1,8,2970), mk(2025,12,31,2950), mk(2025,6,1,2800),
  mk(2025,1,2,2750),
];
pr = Stats.computePeriodReturns(rows, 'date', 'close');
assertItem(pr.ytd, (3000-2950)/2950*100, '2025-12-31', '跨年 ytd 基期取前一年末');
assertItem(pr.today, (3000-2990)/2990*100, '2026-1-14', '今日涨跌');

// 5. 新上市: 只有当年数据 → ytd 回退当年首个交易日
pr = Stats.computePeriodReturns([
  mk(2026,7,10,2.2), mk(2026,7,9,2.1), mk(2026,7,8,2.0), mk(2026,7,7,1.9), mk(2026,7,3,1.8),
], 'date', 'close');
assertItem(pr.ytd, (2.2-1.8)/1.8*100, '2026-7-3', '新上市 ytd 回退当年首日');

// 6. 1 年窗口: 仅取日期 ≤ 最新-365d 的最后一行
const yearRows = [
  mk(2026,8,11,100), mk(2026,8,10,99), mk(2026,8,7,98),
  mk(2025,8,8,90), mk(2025,8,7,89), mk(2025,8,1,88),
];
pr = Stats.computePeriodReturns(yearRows, 'date', 'close');
// 最新 2026-08-11, cutoff = 2025-08-11; 日期 ≤ cutoff 的最后一行 = 2025-08-08(90)
assertItem(pr.y1, (100-90)/90*100, '2025-8-8', '1 年窗口按日历日截断');

// 7. 5 日/20 日/60 日偏移
pr = Stats.computePeriodReturns(rows, 'date', 'close');
assertItem(pr.d5, (3000-2970)/2970*100, '2026-1-8', '5 日(第 5 个交易日)');
check(pr.d20 === null && pr.d60 === null, '数据不足 d20/d60 null');

// 8. 空输入防御
pr = Stats.computePeriodReturns([], 'date', 'close');
check(pr.today === null && pr.ytd === null, '空输入全 null');

console.log(failures === 0 ? 'ALL_OK' : 'HAS_FAILURES');
process.exit(failures === 0 ? 0 : 1);
"""


def test_compute_period_returns_via_node():
    if shutil.which("node") is None:
        pytest.skip("node 不可用, 跳过前端纯函数测试")
    r = subprocess.run(
        ["node", "-e", _NODE_SCRIPT, str(STATS_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert r.returncode == 0, (
        f"node 断言失败:\n{r.stdout}\n{r.stderr}"
    )
    assert "ALL_OK" in r.stdout
