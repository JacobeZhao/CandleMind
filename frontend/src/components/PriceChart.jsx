import React, { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { createChart, LineStyle } from "lightweight-charts";
import { SlidersHorizontal, X, ChevronDown, ChevronUp } from "lucide-react";
import { getKlines } from "../api/client";
import { useApp } from "../context/AppContext";

const INTERVALS   = ["1m", "5m", "15m", "1h", "4h", "1d"];
const VISIBLE_BARS = 80;

// ── Indicator metadata ────────────────────────────────────────────────────────
const IND_META = {
  // Main panel
  supertrend: { name: "SuperTrend",      cat: "趋势", panel: "main", color: "#0ecb81" },
  ema:        { name: "EMA",             cat: "趋势", panel: "main", color: "#3b82f6" },
  sma:        { name: "SMA",             cat: "趋势", panel: "main", color: "#f59e0b" },
  wma:        { name: "WMA",             cat: "趋势", panel: "main", color: "#8b5cf6" },
  dema:       { name: "DEMA",            cat: "趋势", panel: "main", color: "#06b6d4" },
  psar:       { name: "Parabolic SAR",   cat: "趋势", panel: "main", color: "#ec4899" },
  ichimoku:   { name: "Ichimoku",        cat: "趋势", panel: "main", color: "#84cc16" },
  bb:         { name: "Bollinger Bands", cat: "波动", panel: "main", color: "#a855f7" },
  keltner:    { name: "Keltner Channel", cat: "波动", panel: "main", color: "#f97316" },
  donchian:   { name: "Donchian",        cat: "波动", panel: "main", color: "#14b8a6" },
  vwap:       { name: "VWAP",            cat: "成交量",panel: "main", color: "#eab308" },
  // Sub panel
  macd:       { name: "MACD",            cat: "动量", panel: "sub" },
  rsi:        { name: "RSI",             cat: "振荡", panel: "sub" },
  stoch:      { name: "Stochastic",      cat: "振荡", panel: "sub" },
  cci:        { name: "CCI",             cat: "振荡", panel: "sub" },
  williams_r: { name: "Williams %R",     cat: "振荡", panel: "sub" },
  adx:        { name: "ADX",             cat: "动量", panel: "sub" },
  roc:        { name: "ROC",             cat: "动量", panel: "sub" },
  atr:        { name: "ATR",             cat: "波动", panel: "sub" },
  obv:        { name: "OBV",             cat: "成交量",panel: "sub" },
  mfi:        { name: "MFI",             cat: "成交量",panel: "sub" },
  squeeze:    { name: "Squeeze",         cat: "复合", panel: "sub" },
  bb_pctb:    { name: "BB %B",           cat: "复合", panel: "sub" },
};

const DEFAULT_PARAMS = {
  supertrend: { atr_period: 10, multiplier: 3.0 },
  ema:        { period: 20 },
  sma:        { period: 20 },
  wma:        { period: 20 },
  dema:       { period: 20 },
  psar:       { step: 0.02, max: 0.2 },
  ichimoku:   { tenkan: 9, kijun: 26, senkou_b: 52 },
  bb:         { period: 20, std_dev: 2.0 },
  keltner:    { period: 20, multiplier: 2.0 },
  donchian:   { period: 20 },
  vwap:       {},
  macd:       { fast: 12, slow: 26, signal: 9 },
  rsi:        { period: 14 },
  stoch:      { k: 14, d: 3, smooth: 3 },
  cci:        { period: 20 },
  williams_r: { period: 14 },
  adx:        { period: 14 },
  roc:        { period: 12 },
  atr:        { period: 14 },
  obv:        {},
  mfi:        { period: 14 },
  squeeze:    { bb_period: 20, kc_period: 20 },
  bb_pctb:    { period: 20, std_dev: 2.0 },
};

// ── Chart color helpers ───────────────────────────────────────────────────────
const DASHED  = LineStyle.Dashed;
const DOTTED  = LineStyle.Dotted;
const GREEN   = "#0ecb81";
const RED     = "#f6465d";
const MUTED   = "rgba(139,148,178,0.4)";

function band3(chart, scaleId, c, ls = DASHED) {
  const opts = { priceScaleId: scaleId, lastValueVisible: false, priceLineVisible: false };
  return {
    upper:  chart.addLineSeries({ ...opts, color: c, lineWidth: 1, lineStyle: ls }),
    middle: chart.addLineSeries({ ...opts, color: c, lineWidth: 1, lineStyle: DOTTED }),
    lower:  chart.addLineSeries({ ...opts, color: c, lineWidth: 1, lineStyle: ls }),
  };
}

function line(chart, scaleId, c, w = 1, ls = LineStyle.Solid) {
  return chart.addLineSeries({
    priceScaleId: scaleId, color: c, lineWidth: w, lineStyle: ls,
    lastValueVisible: false, priceLineVisible: false,
  });
}

function hist(chart, scaleId, c) {
  return chart.addHistogramSeries({
    priceScaleId: scaleId, color: c,
    lastValueVisible: false, priceLineVisible: false,
  });
}

function setVisible(series, v) {
  if (!series) return;
  if (Array.isArray(series)) series.forEach(s => s?.applyOptions({ visible: v }));
  else if (typeof series === "object" && !series.applyOptions) {
    Object.values(series).forEach(s => s?.applyOptions({ visible: v }));
  } else series.applyOptions({ visible: v });
}

function safeSet(series, data) {
  if (!series || !Array.isArray(data)) return;
  try { series.setData(data); } catch (_) {}
}

function clearData(series) {
  if (!series) return;
  if (Array.isArray(series)) {
    series.forEach(clearData);
  } else if (typeof series === "object" && !series.setData) {
    Object.values(series).forEach(clearData);
  } else {
    safeSet(series, []);
  }
}

// ── Column name helpers ───────────────────────────────────────────────────────
function colName(id, params) {
  const p = { ...DEFAULT_PARAMS[id], ...params };
  const n = p.period || p.atr_period || "";
  switch (id) {
    case "ema":        return `ema${n}`;
    case "sma":        return `sma${n}`;
    case "wma":        return `wma${n}`;
    case "dema":       return `dema${n}`;
    case "rsi":        return `rsi${n}`;
    case "cci":        return `cci${n}`;
    case "williams_r": return `willr${p.period || 14}`;
    case "adx":        return `adx${n}`;
    case "roc":        return `roc${p.period || 12}`;
    case "atr":        return `atr${n}`;
    case "mfi":        return `mfi${n}`;
    default:           return id;
  }
}

function toTs(d) { return Math.floor(new Date(d.open_time).getTime() / 1000); }

// ── Component ─────────────────────────────────────────────────────────────────

export default function PriceChart({ symbol, defaultInterval = "15m" }) {
  const { ticker } = useApp();
  const containerRef = useRef(null);
  const chartRef     = useRef(null);
  const seriesRef    = useRef({});   // { key: series | {upper,middle,lower} }
  const lastBarRef   = useRef(null); // 最后一根 K 线，用于实时更新收盘价

  const [interval,     setIntervalState] = useState(defaultInterval);
  const [loading,      setLoading]       = useState(false);
  const [showPanel,    setShowPanel]     = useState(false);
  const [expandedCat,  setExpandedCat]   = useState({});

  // Main panel: { [id]: { enabled, params } }
  const [mainConf, setMainConf] = useState({
    supertrend: { enabled: true,  params: { atr_period: 10, multiplier: 3.0 } },
    ema:        { enabled: false, params: { period: 20 } },
    sma:        { enabled: false, params: { period: 20 } },
    wma:        { enabled: false, params: { period: 20 } },
    dema:       { enabled: false, params: { period: 20 } },
    bb:         { enabled: false, params: { period: 20, std_dev: 2.0 } },
    keltner:    { enabled: false, params: { period: 20, multiplier: 2.0 } },
    donchian:   { enabled: false, params: { period: 20 } },
    vwap:       { enabled: false, params: {} },
    psar:       { enabled: false, params: { step: 0.02, max: 0.2 } },
    ichimoku:   { enabled: false, params: { tenkan: 9, kijun: 26, senkou_b: 52 } },
  });

  // Sub panel: one at a time
  const [subConf, setSubConf] = useState({ id: null, params: {} });

  // ── Chart init ───────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout:   { background: { color: "#141823" }, textColor: "#8b94b2" },
      grid:     { vertLines: { color: "#1e2436" }, horzLines: { color: "#1e2436" } },
      crosshair: { mode: 1 },
      timeScale: { borderColor: "#1e2436", timeVisible: true },
      width:  containerRef.current.clientWidth,
      height: containerRef.current.clientHeight,
    });

    // Candlestick — add first so "right" scale exists
    seriesRef.current.candle = chart.addCandlestickSeries({
      priceScaleId:    "right",
      upColor:         "rgba(14,203,129,0)",
      downColor:       RED,
      borderUpColor:   GREEN,
      borderDownColor: RED,
      wickUpColor:     GREEN,
      wickDownColor:   RED,
    });
    chart.priceScale("right").applyOptions({
      scaleMargins: { top: 0.03, bottom: 0.35 },
    });

    // Volume — add series first, then configure scale
    seriesRef.current.volume = chart.addHistogramSeries({
      priceScaleId: "vol", color: "rgba(255,255,255,0.08)",
      lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.68, bottom: 0.28 },
    });

    // Sub panel — add a dummy invisible series so scale exists
    const _subDummy = chart.addLineSeries({
      priceScaleId: "sub", color: "transparent", lineWidth: 0,
      lastValueVisible: false, priceLineVisible: false,
    });
    chart.priceScale("sub").applyOptions({
      scaleMargins: { top: 0.76, bottom: 0.02 },
    });

    chartRef.current = chart;

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width:  containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    ro.observe(containerRef.current);
    return () => { ro.disconnect(); chart.remove(); };
  }, []);

  // ── Build indicator request ──────────────────────────────────────────────────
  const indRequest = useMemo(() => {
    const ids    = [];
    const params = {};
    Object.entries(mainConf).forEach(([id, c]) => {
      if (c.enabled) { ids.push(id); params[id] = c.params; }
    });
    if (subConf.id) { ids.push(subConf.id); params[subConf.id] = subConf.params; }
    return { ids, params };
  }, [mainConf, subConf]);

  // ── Data load ────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!symbol || !chartRef.current) return;
    const chart = chartRef.current;
    const sr    = seriesRef.current;
    const controller = new AbortController();

    Object.values(sr).forEach(clearData);
    lastBarRef.current = null;
    setLoading(true);
    getKlines(
      symbol,
      interval,
      200,
      indRequest.ids.length ? indRequest.ids : ["supertrend"],
      indRequest.params,
      controller.signal,
    )
      .then(({ data }) => {
        const ts = (d) => toTs(d);

        // Candle
        const candleData = data.map(d => ({
          time: ts(d), open: d.open, high: d.high, low: d.low, close: d.close,
        }));
        sr.candle.setData(candleData);
        // 记住最后一根 K 线，供实时 ticker 更新收盘价
        lastBarRef.current = candleData.length ? { ...candleData[candleData.length - 1] } : null;

        // Volume
        sr.volume.setData(data.map(d => ({
          time: ts(d), value: d.volume,
          color: d.close >= d.open ? "rgba(14,203,129,0.15)" : "rgba(246,70,93,0.15)",
        })));

        // ── Main panel indicators ─────────────────────────────
        _updateMain(chart, sr, data, mainConf);

        // ── Sub panel indicator ───────────────────────────────
        _updateSub(chart, sr, data, subConf);

        // Visible range
        const total = data.length;
        chart.timeScale().setVisibleLogicalRange({ from: total - VISIBLE_BARS, to: total - 1 });
      })
      .catch((error) => {
        if (error?.code !== "ERR_CANCELED") console.error(error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [symbol, interval, indRequest]);

  // ── 实时价格更新（来自 Binance WS ticker）───────────────────────────────────
  useEffect(() => {
    if (!ticker || ticker.symbol !== symbol) return;
    const bar = lastBarRef.current;
    const candle = seriesRef.current.candle;
    if (!bar || !candle) return;

    const price = parseFloat(ticker.price);
    if (!Number.isFinite(price)) return;

    bar.close = price;
    if (price > bar.high) bar.high = price;
    if (price < bar.low)  bar.low  = price;

    try { candle.update(bar); } catch (_) {}
  }, [ticker, symbol]);

  // ── Toggle helpers ───────────────────────────────────────────────────────────
  const toggleMain = (id) =>
    setMainConf(p => ({ ...p, [id]: { ...p[id], enabled: !p[id].enabled } }));

  const setMainParam = (id, key, val) =>
    setMainConf(p => ({ ...p, [id]: { ...p[id], params: { ...p[id].params, [key]: Number(val) || val } } }));

  const selectSub = (id) =>
    setSubConf(id === subConf.id ? { id: null, params: {} } : { id, params: DEFAULT_PARAMS[id] || {} });

  const setSubParam = (key, val) =>
    setSubConf(p => ({ ...p, params: { ...p.params, [key]: Number(val) || val } }));

  // ── Category grouping ────────────────────────────────────────────────────────
  const mainInds = Object.entries(IND_META).filter(([, m]) => m.panel === "main");
  const subInds  = Object.entries(IND_META).filter(([, m]) => m.panel === "sub");
  const catMap   = {};
  mainInds.forEach(([id, m]) => { catMap[m.cat] = [...(catMap[m.cat] || []), id]; });
  const subCatMap = {};
  subInds.forEach(([id, m]) => { subCatMap[m.cat] = [...(subCatMap[m.cat] || []), id]; });

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-border shrink-0 gap-3">
        <span className="text-sm font-semibold text-white shrink-0">{symbol}</span>

        {/* Active indicator chips */}
        <div className="flex items-center gap-1 flex-wrap flex-1 min-w-0">
          {Object.entries(mainConf)
            .filter(([, c]) => c.enabled)
            .map(([id]) => (
              <span key={id} className="text-xs px-2 py-0.5 rounded-full border"
                style={{ borderColor: `${IND_META[id]?.color}55`, color: IND_META[id]?.color,
                         background: `${IND_META[id]?.color}11` }}>
                {IND_META[id]?.name}
              </span>
            ))}
          {subConf.id && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-surface border border-border text-accent">
              {IND_META[subConf.id]?.name}
            </span>
          )}
        </div>

        {/* Indicator picker button */}
        <button onClick={() => setShowPanel(p => !p)}
          className={`flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg border transition-colors shrink-0 ${
            showPanel ? "border-accent text-accent bg-accent/10" : "border-border text-muted hover:text-white"
          }`}>
          <SlidersHorizontal size={12} /> 指标
        </button>

        {/* Interval tabs */}
        <div className="flex gap-1 shrink-0">
          {INTERVALS.map(iv => (
            <button key={iv} onClick={() => setIntervalState(iv)}
              className={`text-xs px-2 py-1 rounded transition-colors ${
                interval === iv ? "bg-accent text-black font-bold" : "text-muted hover:text-white"
              }`}>
              {iv}
            </button>
          ))}
        </div>
      </div>

      {/* Body: chart + optional indicator panel */}
      <div className="flex flex-1 min-h-0 relative">
        {/* Chart */}
        <div className="relative flex-1 min-w-0">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center bg-card/60 z-10">
              <div className="text-muted text-sm">加载中...</div>
            </div>
          )}
          <div ref={containerRef} className="h-full w-full" />
        </div>

        {/* Indicator panel (right drawer) */}
        {showPanel && (
          <div className="w-56 shrink-0 border-l border-border bg-card flex flex-col overflow-y-auto">
            <div className="flex items-center justify-between px-3 py-2 border-b border-border">
              <span className="text-xs font-semibold text-white">指标选择</span>
              <button onClick={() => setShowPanel(false)} className="text-muted hover:text-white">
                <X size={12} />
              </button>
            </div>

            {/* Main panel indicators by category */}
            <div className="px-2 py-1 border-b border-border">
              <p className="text-[10px] text-muted uppercase tracking-wide px-1 py-1">主图指标</p>
              {Object.entries(catMap).map(([cat, ids]) => (
                <div key={cat}>
                  <button onClick={() => setExpandedCat(p => ({ ...p, [cat]: !p[cat] }))}
                    className="flex items-center justify-between w-full text-xs text-muted hover:text-white px-1 py-1">
                    <span>{cat}</span>
                    {expandedCat[cat] ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                  </button>
                  {expandedCat[cat] && ids.map(id => (
                    <IndRow key={id} id={id} meta={IND_META[id]}
                      conf={mainConf[id]} onToggle={() => toggleMain(id)}
                      onParam={setMainParam} />
                  ))}
                </div>
              ))}
            </div>

            {/* Sub panel indicators */}
            <div className="px-2 py-1">
              <p className="text-[10px] text-muted uppercase tracking-wide px-1 py-1">副图指标（单选）</p>
              {Object.entries(subCatMap).map(([cat, ids]) => (
                <div key={cat}>
                  <button onClick={() => setExpandedCat(p => ({ ...p, [`sub_${cat}`]: !p[`sub_${cat}`] }))}
                    className="flex items-center justify-between w-full text-xs text-muted hover:text-white px-1 py-1">
                    <span>{cat}</span>
                    {expandedCat[`sub_${cat}`] ? <ChevronUp size={10} /> : <ChevronDown size={10} />}
                  </button>
                  {expandedCat[`sub_${cat}`] && ids.map(id => (
                    <SubIndRow key={id} id={id} meta={IND_META[id]}
                      active={subConf.id === id} params={subConf.id === id ? subConf.params : DEFAULT_PARAMS[id]}
                      onSelect={() => selectSub(id)}
                      onParam={setSubParam}
                      isActive={subConf.id === id} />
                  ))}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Indicator row (main panel) ─────────────────────────────────────────────────
function IndRow({ id, meta, conf, onToggle, onParam }) {
  const [open, setOpen] = useState(false);
  const params = conf?.params || DEFAULT_PARAMS[id] || {};
  const hasParams = Object.keys(params).length > 0;

  return (
    <div className="mb-0.5">
      <div className="flex items-center gap-1.5 px-1 py-0.5 rounded hover:bg-surface/50">
        <button onClick={onToggle}
          className={`w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0 transition-colors ${
            conf?.enabled ? "border-transparent" : "border-border bg-transparent"
          }`}
          style={conf?.enabled ? { background: meta?.color || "#3b82f6" } : {}}>
          {conf?.enabled && <span className="text-[9px] text-black font-bold">✓</span>}
        </button>
        <span className="text-xs flex-1 truncate" style={{ color: conf?.enabled ? meta?.color : "#6b7280" }}>
          {meta?.name}
        </span>
        {hasParams && (
          <button onClick={() => setOpen(o => !o)} className="text-muted hover:text-white ml-auto">
            <SlidersHorizontal size={10} />
          </button>
        )}
      </div>
      {open && hasParams && (
        <div className="ml-5 pl-1 border-l border-border/50 space-y-1 py-1">
          {Object.entries(params).map(([k, v]) => (
            <div key={k} className="flex items-center gap-1.5">
              <span className="text-[10px] text-muted w-20 truncate">{k}</span>
              <input type="number" defaultValue={v} step={k.includes("multiplier") || k.includes("std") || k.includes("step") ? 0.1 : 1}
                onChange={e => onParam(id, k, e.target.value)}
                className="w-16 bg-surface border border-border rounded px-1.5 py-0.5 text-xs text-white outline-none" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Indicator row (sub panel) ──────────────────────────────────────────────────
function SubIndRow({ id, meta, active, params, onSelect, onParam }) {
  const [open, setOpen] = useState(false);
  const hasParams = Object.keys(params || {}).length > 0;

  return (
    <div className="mb-0.5">
      <div className="flex items-center gap-1.5 px-1 py-0.5 rounded hover:bg-surface/50">
        <button onClick={onSelect}
          className={`w-3.5 h-3.5 rounded-full border shrink-0 transition-colors ${
            active ? "border-accent bg-accent" : "border-border bg-transparent"
          }`} />
        <span className={`text-xs flex-1 truncate ${active ? "text-white" : "text-muted"}`}>
          {meta?.name}
        </span>
        {active && hasParams && (
          <button onClick={() => setOpen(o => !o)} className="text-muted hover:text-white">
            <SlidersHorizontal size={10} />
          </button>
        )}
      </div>
      {open && active && hasParams && (
        <div className="ml-5 pl-1 border-l border-border/50 space-y-1 py-1">
          {Object.entries(params).map(([k, v]) => (
            <div key={k} className="flex items-center gap-1.5">
              <span className="text-[10px] text-muted w-20 truncate">{k}</span>
              <input type="number" defaultValue={v} step={0.1}
                onChange={e => onParam(k, e.target.value)}
                className="w-16 bg-surface border border-border rounded px-1.5 py-0.5 text-xs text-white outline-none" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Chart series updaters ─────────────────────────────────────────────────────

function _getSeries(chart, sr, key, factory) {
  if (!sr[key]) sr[key] = factory(chart);
  return sr[key];
}

function _updateMain(chart, sr, data, mainConf) {
  Object.entries(mainConf).forEach(([id, conf]) => {
    const visible = conf.enabled;
    const p  = conf.params || {};
    const ts = d => toTs(d);

    switch (id) {
      case "supertrend": {
        const s = _getSeries(chart, sr, "supertrend", c =>
          line(c, "right", GREEN, 2));
        setVisible(s, visible);
        if (visible) {
          safeSet(s, data.filter(d => d.supertrend != null).map(d => ({
            time: ts(d), value: d.supertrend,
            color: d.supertrend_dir === 1 ? GREEN : RED,
          })));
        }
        break;
      }
      case "ema": case "sma": case "wma": case "dema": {
        const col = colName(id, p);
        const clr = IND_META[id].color;
        const s = _getSeries(chart, sr, `${id}_${p.period}`, c => line(c, "right", clr, 1));
        setVisible(s, visible);
        if (visible) safeSet(s, data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
        break;
      }
      case "bb": {
        const s = _getSeries(chart, sr, "bb", c => band3(c, "right", "rgba(168,85,247,0.6)"));
        setVisible(s.upper, visible); setVisible(s.middle, visible); setVisible(s.lower, visible);
        if (visible) {
          safeSet(s.upper,  data.filter(d => d.bb_upper  != null).map(d => ({ time: ts(d), value: d.bb_upper  })));
          safeSet(s.middle, data.filter(d => d.bb_middle != null).map(d => ({ time: ts(d), value: d.bb_middle })));
          safeSet(s.lower,  data.filter(d => d.bb_lower  != null).map(d => ({ time: ts(d), value: d.bb_lower  })));
        }
        break;
      }
      case "keltner": {
        const s = _getSeries(chart, sr, "keltner", c => band3(c, "right", "rgba(249,115,22,0.6)"));
        setVisible(s.upper, visible); setVisible(s.middle, visible); setVisible(s.lower, visible);
        if (visible) {
          safeSet(s.upper,  data.filter(d => d.kc_upper  != null).map(d => ({ time: ts(d), value: d.kc_upper  })));
          safeSet(s.middle, data.filter(d => d.kc_middle != null).map(d => ({ time: ts(d), value: d.kc_middle })));
          safeSet(s.lower,  data.filter(d => d.kc_lower  != null).map(d => ({ time: ts(d), value: d.kc_lower  })));
        }
        break;
      }
      case "donchian": {
        const s = _getSeries(chart, sr, "donchian", c => band3(c, "right", "rgba(20,184,166,0.6)"));
        setVisible(s.upper, visible); setVisible(s.middle, visible); setVisible(s.lower, visible);
        if (visible) {
          safeSet(s.upper,  data.filter(d => d.dc_upper  != null).map(d => ({ time: ts(d), value: d.dc_upper  })));
          safeSet(s.middle, data.filter(d => d.dc_middle != null).map(d => ({ time: ts(d), value: d.dc_middle })));
          safeSet(s.lower,  data.filter(d => d.dc_lower  != null).map(d => ({ time: ts(d), value: d.dc_lower  })));
        }
        break;
      }
      case "vwap": {
        const s = _getSeries(chart, sr, "vwap", c => line(c, "right", "#eab308", 1, DASHED));
        setVisible(s, visible);
        if (visible) safeSet(s, data.filter(d => d.vwap != null).map(d => ({ time: ts(d), value: d.vwap })));
        break;
      }
      case "psar": {
        const s = _getSeries(chart, sr, "psar", c => line(c, "right", "#ec4899", 0));
        s.applyOptions({ visible, pointMarkersVisible: true });
        if (visible) safeSet(s, data.filter(d => d.psar != null).map(d => ({ time: ts(d), value: d.psar })));
        break;
      }
      case "ichimoku": {
        const colors = ["#ef4444","#3b82f6","rgba(14,203,129,0.3)","rgba(239,68,68,0.3)","rgba(107,114,128,0.4)"];
        const keys   = ["ichi_tenkan","ichi_kijun","ichi_senkou_a","ichi_senkou_b","ichi_chikou"];
        const sKey   = (k) => `ichi_${k}`;
        keys.forEach((k, i) => {
          const s = _getSeries(chart, sr, sKey(k), c => line(c, "right", colors[i], 1));
          setVisible(s, visible);
          if (visible) safeSet(s, data.filter(d => d[k] != null).map(d => ({ time: ts(d), value: d[k] })));
        });
        break;
      }
      default: break;
    }
  });
}

function _updateSub(chart, sr, data, subConf) {
  // Hide all existing sub series first
  ["sub_line","sub_line2","sub_line3","sub_hist"].forEach(k => {
    if (sr[k]) setVisible(sr[k], false);
  });

  const { id, params: p } = subConf;
  if (!id) return;

  const ts = d => toTs(d);

  switch (id) {
    case "macd": {
      const sl  = _getSeries(chart, sr, "sub_line",  c => line(c, "sub", "#3b82f6", 1));
      const sl2 = _getSeries(chart, sr, "sub_line2", c => line(c, "sub", "#f97316", 1));
      const sh  = _getSeries(chart, sr, "sub_hist",  c => hist(c, "sub", GREEN));
      setVisible(sl, true); setVisible(sl2, true); setVisible(sh, true);
      safeSet(sl,  data.filter(d => d.macd_line   != null).map(d => ({ time: ts(d), value: d.macd_line })));
      safeSet(sl2, data.filter(d => d.macd_signal != null).map(d => ({ time: ts(d), value: d.macd_signal })));
      safeSet(sh,  data.filter(d => d.macd_hist   != null).map(d => ({
        time: ts(d), value: d.macd_hist,
        color: d.macd_hist >= 0 ? "rgba(14,203,129,0.6)" : "rgba(246,70,93,0.6)",
      })));
      break;
    }
    case "rsi": {
      const col = colName("rsi", p);
      const sl  = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#a855f7", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
      break;
    }
    case "stoch": {
      const sl  = _getSeries(chart, sr, "sub_line",  c => line(c, "sub", "#3b82f6", 1));
      const sl2 = _getSeries(chart, sr, "sub_line2", c => line(c, "sub", "#f97316", 1));
      setVisible(sl, true); setVisible(sl2, true);
      safeSet(sl,  data.filter(d => d.stoch_k != null).map(d => ({ time: ts(d), value: d.stoch_k })));
      safeSet(sl2, data.filter(d => d.stoch_d != null).map(d => ({ time: ts(d), value: d.stoch_d })));
      break;
    }
    case "cci": {
      const col = colName("cci", p);
      const sl  = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#06b6d4", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
      break;
    }
    case "williams_r": {
      const col = colName("williams_r", p);
      const sl  = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#ec4899", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
      break;
    }
    case "adx": {
      const col = colName("adx", p);
      const sl  = _getSeries(chart, sr, "sub_line",  c => line(c, "sub", "#f59e0b", 2));
      const sl2 = _getSeries(chart, sr, "sub_line2", c => line(c, "sub", GREEN, 1));
      const sl3 = _getSeries(chart, sr, "sub_line3", c => line(c, "sub", RED, 1));
      setVisible(sl, true); setVisible(sl2, true); setVisible(sl3, true);
      safeSet(sl,  data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
      safeSet(sl2, data.filter(d => d.pdi  != null).map(d => ({ time: ts(d), value: d.pdi  })));
      safeSet(sl3, data.filter(d => d.ndi  != null).map(d => ({ time: ts(d), value: d.ndi  })));
      break;
    }
    case "roc": {
      const col = colName("roc", p);
      const sh  = _getSeries(chart, sr, "sub_hist", c => hist(c, "sub", GREEN));
      setVisible(sh, true);
      safeSet(sh, data.filter(d => d[col] != null).map(d => ({
        time: ts(d), value: d[col],
        color: d[col] >= 0 ? "rgba(14,203,129,0.6)" : "rgba(246,70,93,0.6)",
      })));
      break;
    }
    case "atr": {
      const col = colName("atr", p);
      const sl  = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#f59e0b", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
      break;
    }
    case "obv": {
      const sl = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#06b6d4", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d.obv != null).map(d => ({ time: ts(d), value: d.obv })));
      break;
    }
    case "mfi": {
      const col = colName("mfi", p);
      const sl  = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#14b8a6", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d[col] != null).map(d => ({ time: ts(d), value: d[col] })));
      break;
    }
    case "squeeze": {
      const sh = _getSeries(chart, sr, "sub_hist", c => hist(c, "sub", GREEN));
      setVisible(sh, true);
      safeSet(sh, data.filter(d => d.sqz_momentum != null).map(d => ({
        time: ts(d), value: d.sqz_momentum,
        color: d.sqz_momentum >= 0
          ? (d.sqz_on ? "rgba(14,203,129,0.8)" : "rgba(14,203,129,0.4)")
          : (d.sqz_on ? "rgba(246,70,93,0.8)"  : "rgba(246,70,93,0.4)"),
      })));
      break;
    }
    case "bb_pctb": {
      const sl = _getSeries(chart, sr, "sub_line", c => line(c, "sub", "#a855f7", 1));
      setVisible(sl, true);
      safeSet(sl, data.filter(d => d.bb_pctb != null).map(d => ({ time: ts(d), value: d.bb_pctb })));
      break;
    }
    default: break;
  }
}
