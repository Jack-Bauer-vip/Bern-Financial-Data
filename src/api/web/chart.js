/* chart.js — ECharts option 纯函数。折线 / K线+成交量。 */
'use strict';

const Charts = (function () {
  const UP = '#d03050';    // 红涨
  const DOWN = '#2f9e44';  // 绿跌
  const GRID = '#e5e7eb';
  const AXIS = '#666';

  function tooltip() {
    return {
      trigger: 'axis',
      backgroundColor: 'rgba(255,255,255,0.96)',
      borderColor: '#e5e7eb',
      textStyle: { color: '#333' },
      confine: true,
      axisPointer: { type: 'cross', label: { backgroundColor: '#94a3b8' } },
    };
  }

  /* 折线图: dates 升序字符串数组; series=[{name, values, color?}] */
  function buildLineOption(dates, series) {
    return {
      animation: false,
      tooltip: tooltip(),
      legend: series.length > 1 ? { top: 0, textStyle: { color: '#555' } } : undefined,
      grid: { left: 64, right: 20, top: series.length > 1 ? 32 : 12, bottom: 34, containLabel: true },
      xAxis: {
        type: 'category', data: dates,
        axisLine: { lineStyle: { color: GRID } },
        axisLabel: { color: AXIS, hideOverlap: true },
      },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: GRID } },
        axisLabel: { color: AXIS },
      },
      dataZoom: [{ type: 'inside', start: 0, end: 100 }],
      series: series.map((s) => ({
        name: s.name, type: 'line',
        showSymbol: dates.length < 120, symbolSize: 4,
        lineStyle: { width: 1.5, color: s.color },
        itemStyle: { color: s.color },
        connectNulls: true,
        data: s.values,
      })),
    };
  }

  /* K线 + 成交量: ohlc={open:[],high:[],low:[],close:[]}, volumes=[] */
  function buildCandlestickOption(dates, ohlc, volumes) {
    const data = ohlc.open.map((_, i) =>
      [ohlc.open[i], ohlc.close[i], ohlc.low[i], ohlc.high[i]]);
    const gridH = volumes.length ? '55%' : '68%';
    return {
      animation: false,
      tooltip: tooltip(),
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: volumes.length
        ? [
            { left: 64, right: 20, top: 12, height: '55%' },
            { left: 64, right: 20, top: '72%', height: '14%' },
          ]
        : [{ left: 64, right: 20, top: 12, height: gridH }],
      xAxis: [
        { type: 'category', data: dates, axisLine: { lineStyle: { color: GRID } }, axisLabel: { color: AXIS, hideOverlap: true } },
        volumes.length ? { type: 'category', data: dates, axisLine: { lineStyle: { color: GRID } }, axisLabel: { show: false }, axisTick: { show: false } } : null,
      ].filter(Boolean),
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: AXIS } },
        volumes.length ? { scale: true, splitLine: { show: false }, axisLabel: { color: AXIS } } : null,
      ].filter(Boolean),
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 30, end: 100 }],
      series: [
        {
          name: 'K线', type: 'candlestick',
          data: data,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        },
        volumes.length ? {
          name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: volumes, barWidth: '60%',
          itemStyle: { color: function (p) { return p.data >= 0 ? 'rgba(208,48,80,.55)' : 'rgba(47,158,68,.55)'; } },
        } : null,
      ].filter(Boolean),
    };
  }

  return { buildLineOption, buildCandlestickOption, UP: UP, DOWN: DOWN };
})();
