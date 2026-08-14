/* app.js — Bern 数据看板装配。纯前端只读, 复用 /api/v1 数据端点。
   理杏仁式: 统计卡片 + 走势图(K线/折线) + 历史表 + URL 深链 + CSV 导出。
   类型预设(通用/指数/ETF/股票) + 代码搜索下拉 + 多周期涨跌幅卡片(阶段一)。 */
'use strict';

(function () {
  const API = '/api/v1';

  // 类型预设: tab → 数据表。general 表任选; index 不支持复权(指数无因子)。
  const PRESETS = {
    general: { table: '', allowAdj: true },
    index: { table: 'index_daily', allowAdj: false },
    etf: { table: 'fund_etf_daily', allowAdj: true },
    stock: { table: 'stock_daily', allowAdj: true },
  };

  const state = {
    type: 'general',
    table: '', searchTable: '',
    code: null, start: null, end: null, limit: 2000, adj: '', transform: '',
    dateCol: null, codeCol: null, ohlc: null, numericCols: [],
    primaryCol: null, chartCol: null, indicatorKeys: [],
    quoteMode: false,   // 行情预设且 OHLC 命中 → 多周期涨跌幅卡片
    rows: [], total: 0,
    page: 1, pageSize: 50, sort: null, sortAsc: false, allCols: false,
    chartMode: 'line', chart: null, hmDate: '',   // hmDate: 热力图当前交易日(空=最新)
    suppressFocusSearch: false,                   // 分类下拉刚渲染时抑制 focus() 触发的默认搜索覆盖
    tables: [], tableNames: {}, deprecated: new Set(), indicatorMap: {},
    indexCategory: '', indexCatMeta: [],   // 指数 tab 分类筛选(''=全部)
  };

  const $ = (id) => document.getElementById(id);
  const els = {};
  let tokenRetry = false;
  let searchTimer = null;
  let dropdownIndex = -1;

  /* ---------- fetch 封装: 带 token / 401 弹窗重试 / 信封解包 ---------- */
  async function fetchJSON(path, params) {
    const qs = params ? '?' + new URLSearchParams(params).toString() : '';
    const headers = {};
    const token = localStorage.getItem('bern_api_token');
    if (token) headers['X-API-Key'] = token;
    let resp;
    try {
      resp = await fetch(API + path + qs, { headers });
    } catch (e) {
      throw new Error('请求失败: ' + e.message);
    }
    if (resp.status === 401) {
      const ok = await askToken();
      if (ok) return fetchJSON(path, params);
      throw new Error('未授权: 缺少 API token');
    }
    if (!resp.ok) {
      let msg = 'HTTP ' + resp.status;
      try { const b = await resp.json(); msg = b.message || b.detail || msg; } catch (e) { /* ignore */ }
      throw new Error(String(msg));
    }
    const body = await resp.json();
    if (body && typeof body === 'object' && 'code' in body && body.code !== 200) {
      throw new Error(body.message || '服务端错误');
    }
    return body;
  }

  /* ---------- 元数据加载 ---------- */
  async function loadMeta() {
    const [tablesBody, sourcesBody, indBody] = await Promise.all([
      fetchJSON('/data/tables'),
      fetchJSON('/sources').catch(() => null),
      fetchJSON('/indicator').catch(() => null),
    ]);
    state.tables = (tablesBody.data || []).map((t) => t.table_name);
    if (sourcesBody) {
      (sourcesBody.data || []).forEach((s) => {
        if (s.table_name) {
          state.tableNames[s.table_name] = s.name || s.table_name;
          if (s.data_status === 'deprecated') state.deprecated.add(s.table_name);
        }
      });
    }
    if (indBody) {
      (indBody.data || []).forEach((it) => {
        if (it.preferred_table && it.indicator_key) {
          if (!state.indicatorMap[it.preferred_table]) state.indicatorMap[it.preferred_table] = [];
          state.indicatorMap[it.preferred_table].push(it.indicator_key);
        }
      });
    }
    populateTableSelect();
  }

  function populateTableSelect() {
    const sel = els.tableSelect;
    sel.innerHTML = '';
    for (const t of state.tables) {
      const name = state.tableNames[t] || t;
      const opt = document.createElement('option');
      opt.value = t;
      opt.textContent = state.deprecated.has(t) ? name + ' ⛔' : name;
      opt.title = t;
      if (state.deprecated.has(t)) opt.style.color = '#b0b7c2';
      sel.appendChild(opt);
    }
  }

  /* ---------- 类型预设 ---------- */
  function applyPreset(type) {
    state.type = type;
    els.typeTabs.querySelectorAll('button[data-type]').forEach((b) => {
      b.classList.toggle('active', b.dataset.type === type);
    });

    // 板块热力图: 独立全屏 treemap 视图, 不涉及表/code/口径
    if (type === 'heatmap') {
      state.quoteMode = false;
      state.code = null; state.codeCol = null;
      state.start = null; state.end = null;   // 日期区间对热力图无意义, 清空防 URL 残留
      els.startDate.value = ''; els.endDate.value = '';
      setHeatmapControls(true);
      els.catChips.hidden = true;
      els.cards.hidden = true;
      els.chartSection.hidden = true;
      els.tableSection.hidden = true;
      els.emptyHint.hidden = true;
      els.heatmapSection.hidden = false;
      loadHeatmap(state.hmDate || '');
      return;
    }
    // 非热力图: 恢复工具栏控件 + 收起热力图区
    setHeatmapControls(false);
    els.heatmapSection.hidden = true;

    // 切类型即重置: 清 code/codeCol/quoteMode/口径(通用模式防残留上一只代码)
    state.quoteMode = false;
    state.code = null; state.codeCol = null;
    els.codeInput.value = '';
    state.adj = ''; els.adjSelect.value = '';
    state.transform = ''; els.transformSelect.value = '';
    state.page = 1; state.sort = null;
    state.indexCategory = '';   // 切类型即重置分类筛选
    els.emptyHint.hidden = true;

    if (type === 'index') {
      // 指数 tab: 显示分类 chip 行, 拉取分类计数(/indices meta.categories)
      loadIndexChips();
    } else {
      els.catChips.hidden = true;
    }

    if (type === 'general') {
      els.tableSelect.disabled = false;
      if (!state.table || !state.tables.includes(state.table)) {
        state.table = state.tables[0] || '';
      }
      els.tableSelect.value = state.table;
    } else {
      state.table = PRESETS[type].table;
      els.tableSelect.value = state.table;
      els.tableSelect.disabled = true;   // 仅 UI 禁用, state.table 恒显式持有
    }
    state.searchTable = state.table;     // 搜索下拉恒指向当前 tab 对应表
  }

  function showEmptyHint(table) {
    els.emptyHint.textContent = '该数据表 ' + table + ' 尚未同步。请先在桌面端(数据→全部同步)同步对应数据源, 再刷新本页。';
    els.emptyHint.hidden = false;
    els.cards.hidden = true;
    els.chartSection.hidden = true;
    els.tableSection.hidden = true;
  }

  /* ---------- 探测表结构 → 设置控件 ---------- */
  async function probeTable() {
    if (!state.table || !state.tables.includes(state.table)) {
      showEmptyHint(state.table || '');
      return;
    }
    els.emptyHint.hidden = true;
    const body = await fetchJSON('/data/' + encodeURIComponent(state.table), { limit: 50 });
    const rows = body.data || [];
    state.rows = rows;
    state.total = body.total || rows.length;
    const cols = rows.length ? Object.keys(rows[0]) : [];
    state.dateCol = Stats.detectDateColumn(cols, rows);
    state.codeCol = Stats.detectCodeColumn(cols);
    state.ohlc = Stats.detectOHLC(cols);
    state.numericCols = Stats.detectNumericColumns(cols, rows);
    state.primaryCol = Stats.pickPrimaryColumn(cols, state.numericCols);
    state.chartCol = state.primaryCol;
    state.indicatorKeys = state.indicatorMap[state.table] || [];
    state.quoteMode = state.type !== 'general' && !!state.ohlc;

    // 控件显隐
    const allowAdj = PRESETS[state.type].allowAdj;
    els.codeInput.hidden = !state.codeCol;
    els.adjSelect.hidden = !(state.ohlc && allowAdj);
    els.transformSelect.hidden = state.indicatorKeys.length === 0;
    els.klineBtn.disabled = !state.ohlc;
    els.colsPicker.hidden = !state.primaryCol;

    // 数值列下拉(单选画图列)
    els.colSelect.innerHTML = '';
    state.numericCols.forEach((c) => {
      const opt = document.createElement('option');
      opt.value = c; opt.textContent = c;
      opt.selected = c === state.primaryCol;
      els.colSelect.appendChild(opt);
    });

    // 多 code 表: 通用模式预填出现频次最高的 code; 行情预设(quoteMode)不自动填, 等用户搜索
    if (state.codeCol && !state.code && !state.quoteMode) {
      const top = Stats.topCode(rows, state.codeCol);
      if (top) { state.code = String(top); els.codeInput.value = state.code; }
    }
    if (state.codeCol) {
      els.codeInput.placeholder = state.quoteMode
        ? '搜索: 代码/名称/拼音'
        : '代码(如 ' + (state.codeCol === 'code' ? '518880' : '600000') + ')';
    }

    await loadData();
  }

  /* ---------- 主数据加载 ---------- */
  async function loadData() {
    if (!state.table) return;
    setLoading(true);
    try {
      // 空日期直接省略, 不传 start_date=null(URLSearchParams 会把 null 序列化成 "null" 导致 422)
      const params = { limit: state.limit };
      if (state.start) params.start_date = state.start;
      if (state.end) params.end_date = state.end;
      // 命中指标键且选了口径 → 走统一指标端点(带派生)
      const usingIndicator = state.transform && state.indicatorKeys.length > 0;
      let body;
      if (usingIndicator) {
        params.transform = state.transform;
        body = await fetchJSON('/indicator/' + encodeURIComponent(state.indicatorKeys[0]), params);
      } else {
        if (state.adj) params.adj = state.adj;
        if (state.code && state.codeCol) params.code = state.code;  // 服务端按 code/symbol/ts_code 过滤
        body = await fetchJSON('/data/' + encodeURIComponent(state.table), params);
      }
      const rows = body.data || [];
      state.rows = rows;
      state.total = body.total || rows.length;

      // 指标端点返回 {date, value}
      if (usingIndicator) {
        state.dateCol = 'date'; state.primaryCol = 'value'; state.chartCol = 'value';
      }

      renderAll();
    } catch (e) {
      toast('加载失败: ' + e.message);
    } finally {
      setLoading(false);
    }
  }

  /* ---------- 渲染 ---------- */
  function renderAll() {
    if (state.type === 'heatmap') return;   // 热力图独立渲染, 不落 cards/chart/table
    renderCards();
    renderChart();
    renderTable();
    renderRangePanel();  // 阶段二预留: 区间统计面板
    syncUrl();
  }

  function renderCards() {
    if (state.quoteMode && state.ohlc) { renderPeriodCards(); return; }
    if (!state.primaryCol) { els.cards.hidden = true; return; }
    const c = Stats.computeCards(state.rows, state.dateCol, state.primaryCol);
    els.cards.hidden = false;
    const momCls = c.mom !== null ? (c.mom >= 0 ? 'up' : 'down') : '';
    const momTxt = c.mom !== null ? (c.mom >= 0 ? '+' : '') + c.mom.toFixed(2) + '%' : '—';
    const yoyTxt = c.yoy !== null ? (c.yoy >= 0 ? '+' : '') + c.yoy.toFixed(2) + '%' : '—';
    const cards = [
      ['最新值', Stats.fmtNumber(c.latest), state.primaryCol],
      ['最新日期', c.latestDate || '—', state.dateCol || ''],
      ['环比%', momTxt, momCls],
      ['同比%(近一年)', yoyTxt, ''],
      ['区间最高', Stats.fmtNumber(c.high), ''],
      ['区间最低', Stats.fmtNumber(c.low), ''],
      ['区间均值', Stats.fmtNumber(c.mean), ''],
    ];
    els.cards.innerHTML = cards.map(([label, val, hint]) =>
      '<div class="card"><div class="label">' + label + '</div><div class="value ' + hint + '">' + val +
      '</div><div class="hint">' + (hint && hint !== label ? (label === '最新值' ? '列: ' + hint : '') : '') + '</div></div>'
    ).join('');
  }

  /* 行情预设专用: 今日/5日/20日/60日/年初至今/1年 涨跌幅卡片。
     注意: 「今日」= 最新交易日相对前一交易日的涨跌, hint 标注最新交易日
     (而非基期), 避免用户误读为前一日行情; 其余周期标注基期日期。 */
  function renderPeriodCards() {
    const pr = Stats.computePeriodReturns(state.rows, state.dateCol, state.ohlc.close);
    els.cards.hidden = false;
    const latestDate = (state.rows.length && state.dateCol) ? state.rows[0][state.dateCol] : null;
    const defs = [
      ['今日', pr.today], ['5日', pr.d5], ['20日', pr.d20], ['60日', pr.d60],
      ['年初至今', pr.ytd], ['1年', pr.y1],
    ];
    els.cards.innerHTML = defs.map(([label, item]) => {
      const pct = item ? (item.pct >= 0 ? '+' : '') + item.pct.toFixed(2) + '%' : '—';
      const cls = item ? (item.pct >= 0 ? 'up' : 'down') : '';
      let hint;
      if (!item) hint = '数据不足';
      else if (label === '今日') hint = '最新 ' + (latestDate || item.baseDate);
      else hint = '基期 ' + (item.baseDate || '—');
      return '<div class="card"><div class="label">' + label + '</div><div class="value ' + cls + '">' + pct +
        '</div><div class="hint">' + hint + '</div></div>';
    }).join('');
  }

  /* 阶段二预留: 区间统计面板(选区间内的涨跌幅/波动率/极值日期等)。
     接入点: renderAll() 中调用; 当前为空桩, 不渲染任何 DOM。下周实现。 */
  function renderRangePanel() {
    return;
  }

  /* ---------- 板块热力图(指数版, 日期筛选 + 概念/行业/核心指数三组网格) ----------
     每组等大小色块(不按成交量分面积): 颜色 = 涨跌幅 红涨绿跌, 深浅 = 幅度;
     成交量/收盘/日期 只在鼠标悬停 tooltip 展示。点色块 → 跳到该指数行情视图。 */
  async function loadHeatmap(date) {
    try {
      const params = {};
      if (date) params.date = date;
      const body = await fetchJSON('/indices/heatmap', params);
      if (state.type !== 'heatmap') return;   // 拉取期间切走了 → 丢弃
      const meta = body.meta || {};
      state.hmDate = meta.date || '';
      populateHeatmapDate(meta.dates || [], state.hmDate);
      renderHeatmapGrid(body.data || [], meta);
    } catch (e) {
      toast('热力图加载失败: ' + e.message);
    }
  }

  function populateHeatmapDate(dates, cur) {
    const sel = els.heatmapDate;
    sel.innerHTML = '';
    (dates || []).forEach((d) => {
      const opt = document.createElement('option');
      opt.value = d; opt.textContent = d;
      opt.selected = d === cur;
      sel.appendChild(opt);
    });
    els.heatmapHead.hidden = dates.length === 0;
  }

  function renderHeatmapGrid(items, meta) {
    const hm = els.heatmap;
    if (!items.length) {
      els.emptyHint.textContent = '该交易日暂无指数行情(index_daily 未同步或各指数停牌)。';
      els.emptyHint.hidden = false;
      hm.innerHTML = '';
      return;
    }
    els.emptyHint.hidden = true;
    const order = ['核心指数', '行业', '概念'];
    const groups = order.map((name) => ({ name, items: items.filter((it) => it.group === name) }))
      .filter((g) => g.items.length);
    const byIdx = {};
    let idx = 0;
    hm.innerHTML = groups.map((g) =>
      '<div class="hm-group">' +
      '<div class="hm-group-title"><span>' + g.name + '</span><em>' + g.items.length + ' 个</em></div>' +
      '<div class="hm-grid">' +
      g.items.map((it) => {
        const cell = hmCellHTML(it, idx);
        byIdx[idx] = it;
        idx += 1;
        return cell;
      }).join('') +
      '</div></div>').join('');

    hm.querySelectorAll('.hm-cell').forEach((cell) => {
      const it = byIdx[+cell.dataset.idx];
      if (!it) return;
      cell.addEventListener('mouseenter', (ev) => showHmTooltip(it, ev));
      cell.addEventListener('mousemove', moveHmTooltip);
      cell.addEventListener('mouseleave', hideHmTooltip);
      cell.addEventListener('click', () => onSelectIndex(it.code));
    });
  }

  function hmCellHTML(it, idx) {
    const pct = Stats.toNumber(it.pct_chg);
    const pctTxt = (pct === null || isNaN(pct)) ? '—' : ((pct >= 0 ? '+' : '') + pct.toFixed(2) + '%');
    const bg = pctColor(pct);
    const fg = contrastText(bg);
    return '<div class="hm-cell" data-idx="' + idx + '" style="background:' + bg + ';color:' + fg + '">' +
      '<div class="hm-name">' + it.name + '</div>' +
      '<div class="hm-pct">' + pctTxt + '</div>' +
      '</div>';
  }

  /* 浅色底用深字, 深色底用白字(与 pctColor 的深浅底纹配套) */
  function contrastText(rgbStr) {
    const m = String(rgbStr).match(/\d+/g);
    if (!m || m.length < 3) return '#1a1a1a';
    const lum = 0.2126 * +m[0] + 0.7152 * +m[1] + 0.0722 * +m[2];
    return lum > 150 ? '#1a1a1a' : '#fff';
  }

  function showHmTooltip(it, ev) {
    const pct = Stats.toNumber(it.pct_chg);
    const cls = (pct !== null && !isNaN(pct)) ? (pct >= 0 ? 'up' : 'down') : '';
    els.hmTooltip.innerHTML =
      '<div class="t-title">' + it.name + ' <span class="' + cls + '">' + fmtPct(it.pct_chg) + '</span></div>' +
      '<div>收盘 ' + Stats.fmtNumber(it.close) + '</div>' +
      '<div>成交量 ' + fmtVol(it.volume) + '</div>' +
      '<div class="t-sub">' + (it.category || '') +
        (it.sub_category ? ' · ' + it.sub_category : '') + ' · ' + (it.date || '') + '</div>';
    els.hmTooltip.hidden = false;
    moveHmTooltip(ev);
  }

  function moveHmTooltip(ev) {
    const t = els.hmTooltip;
    const pad = 14;
    t.style.left = (ev.clientX + pad) + 'px';
    t.style.top = (ev.clientY + pad) + 'px';
    const r = t.getBoundingClientRect();
    if (r.right > window.innerWidth - 8) t.style.left = (ev.clientX - r.width - pad) + 'px';
    if (r.bottom > window.innerHeight - 8) t.style.top = (ev.clientY - r.height - pad) + 'px';
  }

  function hideHmTooltip() { els.hmTooltip.hidden = true; }

  /* 涨跌幅 → 方块颜色: 红涨绿跌, ±5% 封顶饱和, |pct|<0.3 视为平(灰) */
  function pctColor(pct) {
    const v = Stats.toNumber(pct);
    if (v === null || isNaN(v)) return '#e3e6ea';
    const a = Math.abs(v);
    if (a < 0.3) return '#e3e6ea';
    const t = Math.min(1, a / 5);              // 5% 内线性饱和
    if (v > 0) {                               // 浅红 #ef9a9a → 深红 #b71c1c
      const r = Math.round(239 - 56 * t), g = Math.round(154 - 126 * t), b = Math.round(154 - 126 * t);
      return 'rgb(' + r + ',' + g + ',' + b + ')';
    }
    const r = Math.round(165 - 138 * t), g = Math.round(214 - 120 * t), b = Math.round(167 - 135 * t);
    return 'rgb(' + r + ',' + g + ',' + b + ')';   // 浅绿 #a5d6a7 → 深绿 #1b5e20
  }

  function fmtPct(pct) {
    const v = Stats.toNumber(pct);
    if (v === null || isNaN(v)) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  function fmtVol(v) {
    const n = Stats.toNumber(v);
    if (n === null || isNaN(n)) return '—';
    if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿手';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万手';
    return String(Math.round(n)) + ' 手';
  }

  function setHeatmapControls(on) {
    // 热力图不涉及表/日期/code/口径 → 隐藏这些工具栏控件
    const list = [els.tableSelect, els.startDate, els.dateSep, els.endDate,
                  els.codeInput, els.adjSelect, els.transformSelect, els.limitSelect, els.queryBtn];
    list.forEach((el) => { if (el) el.hidden = on; });
    if (on) hideDropdown();
  }

  function onSelectIndex(code) {
    // 点指数方块 → 切到指数预设视图并定位该指数
    applyPreset('index');
    state.code = String(code);
    els.codeInput.value = state.code;
    if (state.table && state.tables.includes(state.table)) {
      probeTable().catch((e) => toast('加载失败: ' + e.message));
    }
  }

  function renderChart() {
    if (!state.dateCol || !state.chartCol) { els.chartSection.hidden = true; return; }
    els.chartSection.hidden = false;
    if (!state.chart) state.chart = echarts.init($('chart'));
    const asc = state.rows.slice().reverse(); // API 日期倒序 → 升序喂图
    const dates = asc.map((r) => Stats.parseDateKey(r[state.dateCol]))
      .map((k) => k ? k.display : '');

    if (state.chartMode === 'k' && state.ohlc) {
      const o = { open: [], high: [], low: [], close: [] };
      asc.forEach((r) => {
        o.open.push(Stats.toNumber(r[state.ohlc.open]));
        o.high.push(Stats.toNumber(r[state.ohlc.high]));
        o.low.push(Stats.toNumber(r[state.ohlc.low]));
        o.close.push(Stats.toNumber(r[state.ohlc.close]));
      });
      const volCol = Stats.findColumn(Object.keys(asc[0] || {}), 'volume');
      const vols = volCol ? asc.map((r) => Stats.toNumber(r[volCol])) : [];
      state.chart.setOption(Charts.buildCandlestickOption(dates, o, vols), true);
    } else {
      const values = asc.map((r) => Stats.toNumber(r[state.chartCol]));
      state.chart.setOption(Charts.buildLineOption(dates, [{ name: state.chartCol, values: values }]), true);
    }
  }

  function renderTable() {
    if (!state.rows.length) { els.tableSection.hidden = true; return; }
    els.tableSection.hidden = false;
    const cols = state.allCols
      ? Object.keys(state.rows[0])
      : Object.keys(state.rows[0]).filter((c) => !['id', 'created_at', 'updated_at'].includes(String(c).toLowerCase()));

    let rows = state.rows.slice();
    if (state.sort) {
      const col = state.sort;
      rows.sort((a, b) => {
        const av = Stats.toNumber(a[col]); const bv = Stats.toNumber(b[col]);
        const useNum = !isNaN(av) && !isNaN(bv);
        let r = useNum ? av - bv : String(a[col] || '').localeCompare(String(b[col] || ''), 'zh');
        return state.sortAsc ? r : -r;
      });
    }
    const totalPages = Math.max(1, Math.ceil(rows.length / state.pageSize));
    state.page = Math.min(state.page, totalPages);
    const slice = rows.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);

    els.tableInfo.textContent = state.table + ' · 已加载 ' + state.rows.length + ' 行 / 全部 ' + state.total + ' 行';
    els.pageInfo.textContent = '第 ' + state.page + ' / ' + totalPages + ' 页 · 每页 ' + state.pageSize + ' 行';
    els.prevPage.disabled = state.page <= 1;
    els.nextPage.disabled = state.page >= totalPages;

    const head = '<tr>' + cols.map((c) =>
      '<th data-col="' + c + '" class="' + (state.sort === c ? 'sorted ' + (state.sortAsc ? 'asc' : 'desc') : '') + '">' + c + '</th>'
    ).join('') + '</tr>';
    const bodyRows = slice.map((r) => {
      const tds = cols.map((c) => {
        const v = r[c];
        const isNum = !isNaN(Stats.toNumber(v)) && v !== null && v !== undefined && String(v).trim() !== '';
        return '<td' + (isNum ? ' class="num"' : '') + '>' + (v === null || v === undefined ? '' : String(v)) + '</td>';
      }).join('');
      return '<tr>' + tds + '</tr>';
    }).join('');
    els.tableWrap.innerHTML = '<table class="dtable"><thead>' + head + '</thead><tbody>' + bodyRows + '</tbody></table>';
  }

  /* ---------- 代码搜索下拉 ---------- */
  async function doSearch(q) {
    if (!state.searchTable) return;
    try {
      const body = await fetchJSON('/search', { q: q, table: state.searchTable, limit: 20 });
      const items = (body.data || []).slice(0, 20);
      renderDropdown(items);
    } catch (e) { /* 搜索失败静默(网络/鉴权弹窗已处理) */ }
  }

  function renderDropdown(items) {
    els.codeDropdown.innerHTML = '';
    dropdownIndex = -1;
    if (!items.length) { els.codeDropdown.hidden = true; return; }
    items.forEach((it) => {
      const d = document.createElement('div');
      d.className = 'opt';
      d.dataset.code = String(it.code);
      const c = document.createElement('span'); c.className = 'code'; c.textContent = it.code;
      const n = document.createElement('span'); n.className = 'name'; n.textContent = it.name || '';
      d.appendChild(c); d.appendChild(n);
      d.addEventListener('mousedown', (ev) => { ev.preventDefault(); selectCode(it.code); });
      els.codeDropdown.appendChild(d);
    });
    els.codeDropdown.hidden = false;
  }

  function hideDropdown() {
    els.codeDropdown.hidden = true;
    dropdownIndex = -1;
  }

  function selectCode(code) {
    state.code = String(code);
    els.codeInput.value = state.code;
    hideDropdown();
    loadData();
  }

  function highlightDropdown(i) {
    const opts = els.codeDropdown.querySelectorAll('.opt');
    dropdownIndex = Math.max(-1, Math.min(i, opts.length - 1));
    opts.forEach((el, idx) => el.classList.toggle('sel', idx === dropdownIndex));
    if (dropdownIndex >= 0 && opts[dropdownIndex]) opts[dropdownIndex].scrollIntoView({ block: 'nearest' });
  }

  /* ---------- 指数分类 chip ---------- */
  async function loadIndexChips() {
    try {
      const body = await fetchJSON('/indices', { limit: 500 });
      state.indexCatMeta = (body.meta && body.meta.categories) || [];
      renderChips();
    } catch (e) { els.catChips.hidden = true; }
  }

  function renderChips() {
    const total = state.indexCatMeta.reduce((s, c) => s + c.count, 0);
    const cats = [{ category: '', count: total }, ...state.indexCatMeta];
    els.catChips.innerHTML = '';
    cats.forEach((c) => {
      const b = document.createElement('button');
      b.className = 'cat-chip' + (state.indexCategory === c.category ? ' active' : '');
      b.dataset.cat = c.category;
      b.textContent = (c.category || '全部') + (c.count ? ' ' + c.count : '');
      b.addEventListener('click', () => selectCategory(c.category));
      els.catChips.appendChild(b);
    });
    els.catChips.hidden = false;
  }

  async function selectCategory(cat) {
    state.indexCategory = cat || '';
    renderChips();
    els.codeInput.value = '';           // 分类浏览 → 清空旧 code, 下拉展示该类指数
    const params = { limit: 200 };
    if (state.indexCategory) params.category = state.indexCategory;
    try {
      const body = await fetchJSON('/indices', params);
      renderDropdown(body.data || []);
      state.suppressFocusSearch = true; // 防 focus() 触发的 doSearch('') 覆盖分类结果
      els.codeInput.focus();            // 保持下拉可见, 可继续 ↑/↓/Enter 选择
    } catch (e) { /* 静默 */ }
  }

  /* ---------- URL 深链 ---------- */
  function syncUrl() {
    const p = new URLSearchParams();
    if (state.type && state.type !== 'general') p.set('type', state.type);
    if (state.table && state.type === 'general') p.set('table', state.table);
    if (state.code) p.set('code', state.code);
    if (state.start) p.set('start', state.start);
    if (state.end) p.set('end', state.end);
    if (state.adj) p.set('adj', state.adj);
    if (state.transform) p.set('transform', state.transform);
    if (state.indexCategory) p.set('category', state.indexCategory);
    if (state.type === 'heatmap' && state.hmDate) p.set('date', state.hmDate);
    p.set('limit', String(state.limit));
    history.replaceState(null, '', '/dashboard?' + p.toString());
  }

  function copyLink() {
    const url = location.origin + '/dashboard?' + new URLSearchParams(location.search).toString();
    navigator.clipboard.writeText(url).then(() => toast('链接已复制')).catch(() => toast('复制失败'));
  }

  /* ---------- token 弹窗 ---------- */
  function askToken() {
    if (tokenRetry) return Promise.resolve(false);
    tokenRetry = true;
    return new Promise((resolve) => {
      els.tokenModal.hidden = false;
      els.tokenInput.value = localStorage.getItem('bern_api_token') || '';
      els.tokenInput.focus();
      const finish = (ok) => { tokenRetry = false; els.tokenModal.hidden = true; resolve(ok); };
      els.tokenOk.onclick = () => {
        localStorage.setItem('bern_api_token', els.tokenInput.value.trim());
        finish(true);
      };
      els.tokenCancel.onclick = () => finish(false);
    });
  }

  /* ---------- 工具 ---------- */
  function toast(msg) {
    els.toast.textContent = msg;
    els.toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { els.toast.hidden = true; }, 2600);
  }

  function setLoading(on) { els.loading.hidden = !on; }

  async function exportCsv() {
    try {
      const params = { format: 'csv', limit: state.limit };
      if (state.start) params.start_date = state.start;
      if (state.end) params.end_date = state.end;
      let url, path;
      if (state.transform && state.indicatorKeys.length) {
        params.transform = state.transform;
        path = '/indicator/' + encodeURIComponent(state.indicatorKeys[0]);
      } else {
        if (state.adj) params.adj = state.adj;
        if (state.code && state.codeCol) params.code = state.code;
        path = '/data/' + encodeURIComponent(state.table);
      }
      url = API + path + '?' + new URLSearchParams(params).toString();
      const headers = {};
      const token = localStorage.getItem('bern_api_token');
      if (token) headers['X-API-Key'] = token;
      const resp = await fetch(url, { headers });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = state.table + '.csv';
      a.click();
      URL.revokeObjectURL(a.href);
      toast('已导出 CSV');
    } catch (e) {
      toast('导出失败: ' + e.message);
    }
  }

  /* ---------- 事件 ---------- */
  function wire() {
    // 类型 tab
    els.typeTabs.addEventListener('click', (ev) => {
      const btn = ev.target.closest('button[data-type]');
      if (!btn || btn.dataset.type === state.type) return;
      applyPreset(btn.dataset.type);
      if (state.type !== 'heatmap' && state.table && state.tables.includes(state.table)) {
        probeTable().catch((e) => toast('加载失败: ' + e.message));
      }
    });

    els.tableSelect.onchange = async () => {
      state.table = els.tableSelect.value;
      state.searchTable = state.table;
      state.code = null; els.codeInput.value = '';
      state.adj = ''; els.adjSelect.value = '';
      state.transform = ''; els.transformSelect.value = '';
      state.chartMode = 'line'; els.klineBtn.classList.remove('active'); els.lineBtn.classList.add('active');
      state.page = 1; state.sort = null;
      try { await probeTable(); } catch (e) { toast('加载失败: ' + e.message); }
    };
    els.queryBtn.onclick = () => {
      state.start = els.startDate.value ? els.startDate.value.replace(/-/g, '') : null;
      state.end = els.endDate.value ? els.endDate.value.replace(/-/g, '') : null;
      state.limit = +els.limitSelect.value;
      state.code = els.codeInput.value.trim() || null;
      state.adj = els.adjSelect.value;
      state.transform = els.transformSelect.value;
      state.page = 1;
      loadData();
    };

    // 代码搜索: 250ms 防抖 → /search; ↑/↓/Enter/Esc; 手输 code 回车保留
    els.codeInput.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => doSearch(els.codeInput.value.trim()), 250);
    });
    els.codeInput.addEventListener('focus', () => {
      if (state.suppressFocusSearch) { state.suppressFocusSearch = false; return; }
      if (!els.codeInput.value && state.searchTable) doSearch('');
    });
    els.codeInput.addEventListener('keydown', (ev) => {
      const open = !els.codeDropdown.hidden;
      if (ev.key === 'ArrowDown' && open) { ev.preventDefault(); highlightDropdown(dropdownIndex + 1); }
      else if (ev.key === 'ArrowUp' && open) { ev.preventDefault(); highlightDropdown(dropdownIndex - 1); }
      else if (ev.key === 'Enter') {
        if (open && dropdownIndex >= 0) {
          ev.preventDefault();
          const opts = els.codeDropdown.querySelectorAll('.opt');
          if (opts[dropdownIndex]) selectCode(opts[dropdownIndex].dataset.code);
        } else {
          hideDropdown();
          els.queryBtn.onclick();   // 无高亮 → 输入原文当精确 code(旧行为)
        }
      }
      else if (ev.key === 'Escape') { hideDropdown(); }
    });
    document.addEventListener('click', (ev) => {
      if (!els.searchbox.contains(ev.target)) hideDropdown();
    });

    els.adjSelect.onchange = els.queryBtn.onclick;
    els.transformSelect.onchange = els.queryBtn.onclick;
    els.limitSelect.onchange = els.queryBtn.onclick;
    els.colSelect.onchange = () => { state.chartCol = els.colSelect.value; renderChart(); };
    els.lineBtn.onclick = () => { state.chartMode = 'line'; els.lineBtn.classList.add('active'); els.klineBtn.classList.remove('active'); renderChart(); };
    els.klineBtn.onclick = () => { state.chartMode = 'k'; els.klineBtn.classList.add('active'); els.lineBtn.classList.remove('active'); renderChart(); };
    els.showAllCols.onchange = () => { state.allCols = els.showAllCols.checked; renderTable(); };
    els.prevPage.onclick = () => { state.page = Math.max(1, state.page - 1); renderTable(); };
    els.nextPage.onclick = () => { state.page += 1; renderTable(); };
    els.exportCsvBtn.onclick = exportCsv;
    els.copyLinkBtn.onclick = copyLink;

    els.tableWrap.onclick = (ev) => {
      const th = ev.target.closest('th[data-col]');
      if (!th) return;
      const col = th.dataset.col;
      if (state.sort === col) state.sortAsc = !state.sortAsc;
      else { state.sort = col; state.sortAsc = true; }
      state.page = 1;
      renderTable();
    };
    // 热力图: 切换交易日 → 重拉 + 更新 URL 深链
    els.heatmapDate.onchange = () => {
      state.hmDate = els.heatmapDate.value;
      loadHeatmap(state.hmDate);
      syncUrl();
    };
    window.addEventListener('resize', () => {
      if (state.chart) state.chart.resize();
    });
  }

  /* ---------- 初始化 ---------- */
  async function init() {
    Object.assign(els, {
      typeTabs: $('type-tabs'), tableSelect: $('table-select'), startDate: $('start-date'), endDate: $('end-date'),
      codeInput: $('code-input'), codeDropdown: $('code-dropdown'), searchbox: $('searchbox'),
      adjSelect: $('adj-select'), transformSelect: $('transform-select'),
      limitSelect: $('limit-select'), queryBtn: $('query-btn'), copyLinkBtn: $('copy-link-btn'),
      cards: $('cards'), chartSection: $('chart-section'), chart: $('chart'),
      lineBtn: $('chart-mode-line'), klineBtn: $('chart-mode-k'),
      colsPicker: $('cols-picker'), colSelect: $('col-select'),
      tableSection: $('table-section'), tableInfo: $('table-info'), tableWrap: $('table-wrap'),
      pageInfo: $('page-info'), prevPage: $('prev-page'), nextPage: $('next-page'),
      showAllCols: $('show-all-cols'), exportCsvBtn: $('export-csv-btn'),
      catChips: $('cat-chips'), emptyHint: $('empty-hint'),
      heatmapSection: $('heatmap-section'), heatmap: $('heatmap'),
      heatmapHead: $('heatmap-head'), heatmapDate: $('heatmap-date'), hmTooltip: $('hm-tooltip'),
      dateSep: $('date-sep'),
      loading: $('loading'), tokenModal: $('token-modal'), tokenInput: $('token-input'),
      tokenOk: $('token-ok'), tokenCancel: $('token-cancel'), toast: $('toast'),
    });

    wire();
    try {
      await loadMeta();
    } catch (e) {
      toast('初始化失败: ' + e.message);
      return;
    }
    // 读 URL 深链: 优先 type 预设, 再叠加 code/start/end/adj/limit
    const q = new URLSearchParams(location.search);
    const wantType = q.get('type');
    if (wantType === 'heatmap') {
      state.hmDate = q.get('date') || '';   // 深链: 板块热力图(可带 ?date= 指定交易日)
      applyPreset('heatmap');
      return;
    }
    const typeValid = wantType && PRESETS[wantType] &&
      (wantType === 'general' || state.tables.includes(PRESETS[wantType].table));
    if (typeValid) {
      applyPreset(wantType);
      if (wantType === 'general') {
        const wt = q.get('table');
        if (wt && state.tables.includes(wt)) { state.table = wt; els.tableSelect.value = wt; state.searchTable = wt; }
      }
      if (q.get('code')) { state.code = q.get('code'); els.codeInput.value = state.code; }
      if (q.get('start')) { state.start = q.get('start'); els.startDate.value = q.get('start'); }
      if (q.get('end')) { state.end = q.get('end'); els.endDate.value = q.get('end'); }
      if (q.get('adj')) { state.adj = q.get('adj'); els.adjSelect.value = state.adj; }
      if (q.get('transform')) { state.transform = q.get('transform'); els.transformSelect.value = state.transform; }
      if (q.get('limit')) { state.limit = +q.get('limit'); els.limitSelect.value = String(state.limit); }
      if (wantType === 'index' && q.get('category')) {
        state.indexCategory = q.get('category');
        selectCategory(state.indexCategory);   // 恢复分类筛选(下拉展示该类指数)
      }
      if (state.table && state.tables.includes(state.table)) {
        try { await probeTable(); } catch (e) { toast('加载失败: ' + e.message); }
      }
      return;
    }

    // 无 type → 现有逻辑(旧链接零破坏)
    const wantTable = q.get('table');
    if (wantTable && state.tables.includes(wantTable)) {
      els.tableSelect.value = wantTable;
      state.table = wantTable;
      state.searchTable = wantTable;
      if (q.get('code')) { state.code = q.get('code'); els.codeInput.value = state.code; }
      if (q.get('start')) { state.start = q.get('start'); els.startDate.value = q.get('start'); }
      if (q.get('end')) { state.end = q.get('end'); els.endDate.value = q.get('end'); }
      if (q.get('adj')) { state.adj = q.get('adj'); els.adjSelect.value = state.adj; }
      if (q.get('transform')) { state.transform = q.get('transform'); els.transformSelect.value = state.transform; }
      if (q.get('limit')) { state.limit = +q.get('limit'); els.limitSelect.value = String(state.limit); }
      try { await probeTable(); } catch (e) { toast('加载失败: ' + e.message); }
    } else {
      els.tableSelect.value = state.tables[0] || '';
      state.table = state.tables[0] || '';
      state.searchTable = state.table;
      if (state.table) { try { await probeTable(); } catch (e) { toast('加载失败: ' + e.message); } }
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
