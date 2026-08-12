/* stats.js — 看板统计纯函数(无 DOM)。列识别 / 卡片统计 / 日期归一化。
   与后端启发式对齐: src/gui/table_view.py 列重排、repository.py 数值列解析。 */
'use strict';

const Stats = (function () {
  const DATE_KEYWORDS = ['date', '时间', '日期', '月份', '季度', '年月', 'trade_date', 'datetime', 'trd_date'];
  const CODE_KEYWORDS = ['code', 'symbol', 'ts_code'];
  const META_COLS = ['id', 'created_at', 'updated_at'];
  const NUMERIC_EXCLUDE = ['code', 'symbol', 'ts_code', 'id', 'created_at', 'updated_at',
    'date', '时间', '日期', '月份', '季度', '名称', '商品', '指标', '发布日期', '公告日期'];

  /* 转数字: 兼容 TEXT 字符串('101.0'、'1,234.5'、'3.5%')。失败返回 NaN */
  function toNumber(s) {
    if (s === null || s === undefined || s === '') return NaN;
    if (typeof s === 'number') return isFinite(s) ? s : NaN;
    const t = String(s).replace(/[,\s%¥￥$]/g, '').trim();
    if (!t) return NaN;
    const n = parseFloat(t);
    return isFinite(n) ? n : NaN;
  }

  function looksLikeDate(s) {
    if (s === null || s === undefined) return false;
    const t = String(s).trim();
    if (!t) return false;
    if (/^\d{4}年/.test(t)) return true;
    if (/^\d{4}[-/.]\d{1,2}/.test(t)) return true;
    return !isNaN(Date.parse(t));
  }

  /* 解析中文/ISO 日期 → {key, display, sort}。无法解析 → null */
  function parseDateKey(s) {
    if (s === null || s === undefined || s === '') return null;
    const t = String(s).trim();
    // 2026-08-11 / 2026-08 / 2026-8-1
    let m = t.match(/^(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?/);
    if (m) {
      const y = m[1], mo = String(+m[2]).padStart(2, '0');
      const d = m[3] ? String(+m[3]).padStart(2, '0') : null;
      return {
        key: d ? y + '-' + mo + '-' + d : y + '-' + mo,
        display: d ? y + '-' + mo + '-' + d : y + '-' + mo,
        sort: +(y + mo + (d ? (+m[3] < 10 ? '0' + (+m[3]) : m[3]) : '00')),
      };
    }
    // 2026年06月份 / 2026年06月 / 2026年06
    m = t.match(/^(\d{4})年(\d{1,2})月/);
    if (m) {
      const mo = String(+m[2]).padStart(2, '0');
      return { key: m[1] + '-' + mo, display: m[1] + '-' + mo, sort: +(m[1] + mo) };
    }
    // 2026年一季度 / 2026年第3季度 / 2026年4季度
    m = t.match(/^(\d{4})年?[第]?([一二三四1-4])季/);
    if (m) {
      const q = { '一': 1, '二': 2, '三': 3, '四': 4 }[m[2]] || +m[2];
      return { key: m[1] + '-Q' + q, display: m[1] + 'Q' + q, sort: +(m[1] + q) };
    }
    // 2026年
    m = t.match(/^(\d{4})年?$/);
    if (m) return { key: m[1], display: m[1], sort: +m[1] };
    return null;
  }

  /* 时间列识别: 关键词优先(与后端一致), 兜底样本解析 */
  function detectDateColumn(columns, rows) {
    const low = {};
    columns.forEach((c) => { low[String(c).toLowerCase()] = c; });
    for (const kw of DATE_KEYWORDS) {
      if (low[kw] !== undefined) return low[kw];
    }
    for (const c of columns) {
      const s = String(c).toLowerCase();
      if (DATE_KEYWORDS.some((kw) => s.includes(kw))) return c;
    }
    for (const c of columns) {
      if (META_COLS.includes(String(c).toLowerCase())) continue;
      const samples = rows.slice(0, 20).map((r) => r[c]);
      if (samples.length && samples.filter(looksLikeDate).length >= samples.length * 0.5) return c;
    }
    return null;
  }

  function detectCodeColumn(columns) {
    const low = {};
    columns.forEach((c) => { low[String(c).toLowerCase()] = c; });
    for (const kw of CODE_KEYWORDS) if (low[kw] !== undefined) return low[kw];
    return null;
  }

  /* OHLC 行情列检测 */
  function detectOHLC(columns) {
    const low = {};
    columns.forEach((c) => { low[String(c).toLowerCase()] = c; });
    const need = ['open', 'high', 'low', 'close'];
    if (need.every((k) => low[k] !== undefined)) {
      return { open: low.open, high: low.high, low: low.low, close: low.close };
    }
    return null;
  }

  function findColumn(columns, name) {
    const low = {};
    columns.forEach((c) => { low[String(c).toLowerCase()] = c; });
    return low[String(name).toLowerCase()] || null;
  }

  /* 数值列识别: 排除元数据/时间/code 列, 非空样本 ≥60% 可转数字 */
  function detectNumericColumns(columns, rows) {
    const out = [];
    for (const c of columns) {
      const s = String(c).toLowerCase();
      if (NUMERIC_EXCLUDE.includes(s)) continue;
      if (DATE_KEYWORDS.some((kw) => s.includes(kw))) continue;
      const samples = rows.slice(0, 50).map((r) => r[c]);
      const nonEmpty = samples.filter((v) => v !== null && v !== undefined && String(v).trim() !== '');
      if (nonEmpty.length === 0) continue;
      const numeric = nonEmpty.filter((v) => !isNaN(toNumber(v)));
      if (numeric.length / nonEmpty.length >= 0.6) out.push(c);
    }
    return out;
  }

  /* 主数值列: close → value → 含同比/今值/现值 → 首个数值列(与 repository 口径一致) */
  function pickPrimaryColumn(columns, numericCols) {
    const hit = findColumn(columns, 'close') || findColumn(columns, 'value');
    if (hit) return hit;
    for (const kw of ['同比', '今值', '现值']) {
      const c = columns.find((x) => String(x).includes(kw));
      if (c) return c;
    }
    return numericCols[0] || null;
  }

  /* 卡片统计。rows 为日期倒序(API 默认)。字段缺失用 null。 */
  function computeCards(rows, dateCol, valCol) {
    const out = { latest: null, latestDate: null, mom: null, yoy: null, high: null, low: null, mean: null, count: 0 };
    const vals = [];
    let latestIdx = -1;
    for (let i = 0; i < rows.length; i++) {
      const n = toNumber(rows[i][valCol]);
      if (!isNaN(n)) {
        vals.push(n);
        if (latestIdx === -1) latestIdx = i;
      }
    }
    out.count = vals.length;
    if (vals.length === 0) return out;
    out.latest = vals[0];
    if (latestIdx >= 0 && dateCol) out.latestDate = rows[latestIdx][dateCol];
    out.high = Math.max.apply(null, vals);
    out.low = Math.min.apply(null, vals);
    out.mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    if (vals.length >= 2 && vals[1] !== 0) {
      out.mom = (vals[0] - vals[1]) / Math.abs(vals[1]) * 100;
    }
    out.yoy = computeYoy(rows, dateCol, valCol);
    return out;
  }

  /* 同比(尽力而为): 找最新期往前 12 个周期的值。非严格, 找不到返回 null。 */
  function computeYoy(rows, dateCol, valCol) {
    if (!dateCol) return null;
    const latest = parseDateKey(rows[0] && rows[0][dateCol]);
    if (!latest || latest.key.length < 7) return null;
    const base = latest.key.slice(0, 7); // YYYY-MM
    const targetYear = +base.slice(0, 4) - 1;
    const targetPrefix = String(targetYear) + base.slice(4); // YYYY-MM 前一年同月
    let prevVal = null;
    for (const r of rows) {
      const pk = parseDateKey(r[dateCol]);
      if (pk && pk.key.startsWith(targetPrefix)) {
        const n = toNumber(r[valCol]);
        if (!isNaN(n)) { prevVal = n; break; }
      }
    }
    if (prevVal === null || prevVal === 0 || isNaN(toNumber(rows[0][valCol]))) return null;
    return (toNumber(rows[0][valCol]) - prevVal) / Math.abs(prevVal) * 100;
  }

  /* 多周期涨跌幅(行情预设卡片)。rows 为日期倒序(API 默认, 最新在前)。
     返回 {today,d5,d20,d60,ytd,y1}, 每项为 {pct, baseDate} 或 null。
     - today/d5/d20/d60: 以第 1/5/20/60 个交易日为基期
     - ytd: 前一年末 → 当年首个交易日 → 表内最早日期 三段回退(新上市标的)
     - y1: 日期 ≤ (最新日 - 365天) 的最后一行(日历日基准, 兼容停牌)
     - 最新值无效(<2) → 全部 null; 基期 close=0/NaN → 对应项 null */
  function computePeriodReturns(rows, dateCol, valCol) {
    const out = { today: null, d5: null, d20: null, d60: null, ytd: null, y1: null };
    if (!rows || !rows.length || !valCol) return out;
    const latest = toNumber(rows[0][valCol]);
    if (!isFinite(latest) || latest < 2) return out;

    const latestKey = dateCol ? parseDateKey(rows[0][dateCol]) : null;
    const pct = (base) => (latest - base) / Math.abs(base) * 100;

    const atOffset = (n) => {
      if (rows.length <= n) return null;
      const base = toNumber(rows[n][valCol]);
      if (!isFinite(base) || base === 0) return null;
      return { pct: pct(base), baseDate: rows[n][dateCol] };
    };
    out.today = atOffset(1);
    out.d5 = atOffset(5);
    out.d20 = atOffset(20);
    out.d60 = atOffset(60);

    // 年初至今
    if (latestKey && latestKey.key.length >= 4) {
      const y = +latestKey.key.slice(0, 4);
      let base = null;
      // ① 前一年最后一个交易日(desc 序中首个 year == y-1)
      for (let i = 0; i < rows.length; i++) {
        const k = parseDateKey(rows[i][dateCol]);
        if (k && +k.key.slice(0, 4) === y - 1) { base = { idx: i, date: rows[i][dateCol] }; break; }
      }
      if (!base) {
        // ② 当年首个交易日(desc 序中最后一个 year == y)
        for (let i = rows.length - 1; i >= 0; i--) {
          const k = parseDateKey(rows[i][dateCol]);
          if (k && +k.key.slice(0, 4) === y) { base = { idx: i, date: rows[i][dateCol] }; break; }
        }
      }
      if (!base) {
        // ③ 表内最早日期
        base = { idx: rows.length - 1, date: rows[rows.length - 1][dateCol] };
      }
      // base.idx === 0 说明基期就是最新行本身(只有一行数据) → 视为数据不足
      if (base.idx !== 0) {
        const b = toNumber(rows[base.idx][valCol]);
        if (isFinite(b) && b !== 0) out.ytd = { pct: pct(b), baseDate: base.date };
      }
    }

    // 1年
    if (latestKey && dateCol && latestKey.key.length >= 10) {
      const latestMs = new Date(latestKey.key.slice(0, 10)).getTime();
      if (!isNaN(latestMs)) {
        const cutoff = latestMs - 365 * 24 * 3600 * 1000;
        for (let i = 0; i < rows.length; i++) {
          const k = parseDateKey(rows[i][dateCol]);
          if (k && k.key.length >= 10) {
            const t = new Date(k.key.slice(0, 10)).getTime();
            if (!isNaN(t) && t <= cutoff) {
              const b = toNumber(rows[i][valCol]);
              if (isFinite(b) && b !== 0) out.y1 = { pct: pct(b), baseDate: rows[i][dateCol] };
              break;
            }
          }
        }
      }
    }
    return out;
  }

  /* 多 code 表: 取最近 200 行中出现频次最高的 code 作为默认 */
  function topCode(rows, codeCol) {
    const freq = {};
    for (const r of rows) {
      const v = r[codeCol];
      if (v === null || v === undefined || v === '') continue;
      freq[v] = (freq[v] || 0) + 1;
    }
    let best = null, bestN = 0;
    for (const k in freq) {
      if (freq[k] > bestN) { best = k; bestN = freq[k]; }
    }
    return best;
  }

  function fmtNumber(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const d = digits === undefined ? (Math.abs(n) < 10000 ? 4 : 2) : digits;
    return n.toLocaleString('zh-CN', { maximumFractionDigits: d, minimumFractionDigits: 0 });
  }

  return {
    toNumber, parseDateKey, looksLikeDate,
    detectDateColumn, detectCodeColumn, detectOHLC, findColumn,
    detectNumericColumns, pickPrimaryColumn, computeCards, computeYoy,
    computePeriodReturns, topCode, fmtNumber,
  };
})();
