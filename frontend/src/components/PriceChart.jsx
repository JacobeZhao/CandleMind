import React, { useCallback, useEffect, useRef, useState } from "react";
import { MessageSquare } from "lucide-react";
import { createChart, LineStyle } from "lightweight-charts";
import { getKlines } from "../api/client";
import { getTickerSnapshot, subscribeTicker } from "../context/MarketTickerContext";

const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"];
const VISIBLE_BARS = 80;
const BOUNDARY_REFRESH_DELAY_MS = 250;
const BOUNDARY_RETRY_DELAY_MS = 500;
const MAX_BOUNDARY_ATTEMPTS_PER_EVENT = 2;
const FIXED_INDICATORS = ["psar", "adx"];
const FIXED_INDICATOR_PARAMS = {
  psar: { step: 0.02, max: 0.2 },
  adx: { period: 14 },
};
const INTERVAL_MS = {
  "1m": 60_000,
  "5m": 5 * 60_000,
  "15m": 15 * 60_000,
  "1h": 60 * 60_000,
  "4h": 4 * 60 * 60_000,
  "1d": 24 * 60 * 60_000,
};

const GREEN = "#0ecb81";
const RED = "#f6465d";

export function intervalBucketStart(eventTime, interval) {
  const timestamp = Number(eventTime);
  const duration = INTERVAL_MS[interval];
  if (!Number.isFinite(timestamp) || !duration) return null;
  return Math.floor(timestamp / duration) * duration / 1000;
}

function toTimestamp(row) {
  return Math.floor(new Date(row.open_time).getTime() / 1000);
}

function line(chart, scaleId, color, lineWidth = 1) {
  return chart.addLineSeries({
    priceScaleId: scaleId,
    color,
    lineWidth,
    lineStyle: LineStyle.Solid,
    lastValueVisible: false,
    priceLineVisible: false,
  });
}

function points(chart, color) {
  return chart.addLineSeries({
    priceScaleId: "right",
    color,
    lineVisible: false,
    pointMarkersVisible: true,
    pointMarkersRadius: 2,
    lastValueVisible: false,
    priceLineVisible: false,
  });
}

function safeSet(series, data) {
  try {
    series?.setData(Array.isArray(data) ? data : []);
  } catch (_) {
    // Ignore chart updates during teardown.
  }
}

function clearSeries(series) {
  Object.values(series).forEach((item) => safeSet(item, []));
}

function updateFixedIndicators(series, data) {
  const timestamped = (column) => data
    .filter((row) => row[column] != null)
    .map((row) => ({ time: toTimestamp(row), value: row[column] }));

  safeSet(
    series.psarBull,
    data
      .filter((row) => row.psar != null && row.psar_direction === 1)
      .map((row) => ({ time: toTimestamp(row), value: row.psar })),
  );
  safeSet(
    series.psarBear,
    data
      .filter((row) => row.psar != null && row.psar_direction === -1)
      .map((row) => ({ time: toTimestamp(row), value: row.psar })),
  );
  safeSet(series.adx, timestamped("adx14"));
  safeSet(series.plusDi, timestamped("pdi"));
  safeSet(series.minusDi, timestamped("ndi"));
}

export default function PriceChart({
  symbol,
  interval,
  onIntervalChange,
  onOpenAssistant,
  assistantOpen = false,
  headerLeading = null,
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef({});
  const lastBarRef = useRef(null);
  const requestRef = useRef({ generation: 0, controller: null });
  const resizeRef = useRef({ width: 0, height: 0 });
  const resizeFrameRef = useRef(null);
  const boundaryRef = useRef({
    key: null,
    targetBucket: null,
    timer: null,
    inFlight: false,
    attempts: 0,
    complete: false,
  });
  const scheduleBoundaryRef = useRef(null);

  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return undefined;

    const initialWidth = containerRef.current.clientWidth;
    const initialHeight = containerRef.current.clientHeight;
    const chart = createChart(containerRef.current, {
      layout: { background: { color: "#141823" }, textColor: "#8b94b2" },
      grid: { vertLines: { color: "#1e2436" }, horzLines: { color: "#1e2436" } },
      crosshair: { mode: 1 },
      timeScale: { borderColor: "#1e2436", timeVisible: true },
      width: initialWidth,
      height: initialHeight,
    });

    const series = seriesRef.current;
    series.candle = chart.addCandlestickSeries({
      priceScaleId: "right",
      upColor: "rgba(14,203,129,0)",
      downColor: RED,
      borderUpColor: GREEN,
      borderDownColor: RED,
      wickUpColor: GREEN,
      wickDownColor: RED,
    });
    chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.03, bottom: 0.35 } });

    series.volume = chart.addHistogramSeries({
      priceScaleId: "vol",
      color: "rgba(255,255,255,0.08)",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.68, bottom: 0.28 } });

    line(chart, "sub", "transparent", 0);
    chart.priceScale("sub").applyOptions({ scaleMargins: { top: 0.76, bottom: 0.02 } });
    series.psarBull = points(chart, GREEN);
    series.psarBear = points(chart, RED);
    series.adx = line(chart, "sub", "#f59e0b", 2);
    series.plusDi = line(chart, "sub", GREEN);
    series.minusDi = line(chart, "sub", RED);

    chartRef.current = chart;
    resizeRef.current = { width: initialWidth, height: initialHeight };

    const observer = new ResizeObserver(() => {
      if (resizeFrameRef.current != null) return;
      resizeFrameRef.current = requestAnimationFrame(() => {
        resizeFrameRef.current = null;
        const node = containerRef.current;
        if (!node) return;
        const next = { width: node.clientWidth, height: node.clientHeight };
        const previous = resizeRef.current;
        if (next.width === previous.width && next.height === previous.height) return;
        resizeRef.current = next;
        chart.applyOptions(next);
      });
    });
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      if (resizeFrameRef.current != null) cancelAnimationFrame(resizeFrameRef.current);
      resizeFrameRef.current = null;
      chartRef.current = null;
      chart.remove();
    };
  }, []);

  const loadData = useCallback((initialLoad) => {
    if (!symbol || !chartRef.current) return null;

    const chart = chartRef.current;
    const series = seriesRef.current;
    const controller = new AbortController();
    requestRef.current.controller?.abort();
    const generation = requestRef.current.generation + 1;
    requestRef.current = { generation, controller };

    if (initialLoad) {
      clearSeries(series);
      lastBarRef.current = null;
      setLoading(true);
    }

    const timeScale = chart.timeScale();
    const previousRange = initialLoad ? null : timeScale.getVisibleLogicalRange?.();
    const completion = getKlines(
      symbol,
      interval,
      200,
      FIXED_INDICATORS,
      FIXED_INDICATOR_PARAMS,
      controller.signal,
    )
      .then(({ data }) => {
        if (controller.signal.aborted || requestRef.current.generation !== generation) return null;

        const candleData = data.map((row) => ({
          time: toTimestamp(row),
          open: row.open,
          high: row.high,
          low: row.low,
          close: row.close,
        }));
        safeSet(series.candle, candleData);
        lastBarRef.current = candleData.length ? { ...candleData.at(-1) } : null;
        safeSet(series.volume, data.map((row) => ({
          time: toTimestamp(row),
          value: row.volume,
          color: row.close >= row.open
            ? "rgba(14,203,129,0.15)"
            : "rgba(246,70,93,0.15)",
        })));
        updateFixedIndicators(series, data);

        if (initialLoad) {
          const total = data.length;
          timeScale.setVisibleLogicalRange({ from: total - VISIBLE_BARS, to: total - 1 });
        } else if (previousRange) {
          timeScale.setVisibleLogicalRange(previousRange);
        }
        return lastBarRef.current?.time ?? null;
      })
      .catch((error) => {
        if (error?.code !== "ERR_CANCELED") console.error(error);
        return null;
      })
      .finally(() => {
        if (
          initialLoad
          && !controller.signal.aborted
          && requestRef.current.generation === generation
        ) {
          setLoading(false);
        }
      });

    return { controller, completion };
  }, [symbol, interval]);

  const resetBoundaryRefresh = useCallback(() => {
    const current = boundaryRef.current;
    if (current.timer != null) window.clearTimeout(current.timer);
    boundaryRef.current = {
      key: null,
      targetBucket: null,
      timer: null,
      inFlight: false,
      attempts: 0,
      complete: false,
    };
  }, []);

  const scheduleBoundaryRefresh = useCallback((targetBucket, fromTickerEvent) => {
    const key = `${symbol}:${interval}:${targetBucket}`;
    let current = boundaryRef.current;
    if (current.key !== key) {
      if (current.timer != null) window.clearTimeout(current.timer);
      current = {
        key,
        targetBucket,
        timer: null,
        inFlight: false,
        attempts: 0,
        complete: false,
      };
      boundaryRef.current = current;
    }
    if (current.complete || current.inFlight || current.timer != null) return;

    if (fromTickerEvent && current.attempts >= MAX_BOUNDARY_ATTEMPTS_PER_EVENT) {
      current.attempts = 0;
    }
    if (!fromTickerEvent && current.attempts >= MAX_BOUNDARY_ATTEMPTS_PER_EVENT) return;

    const delay = current.attempts === 0
      ? BOUNDARY_REFRESH_DELAY_MS
      : BOUNDARY_RETRY_DELAY_MS;
    current.timer = window.setTimeout(async () => {
      const active = boundaryRef.current;
      if (active.key !== key || active.complete) return;
      active.timer = null;
      active.inFlight = true;
      active.attempts += 1;

      const request = loadData(false);
      const loadedBucket = request ? await request.completion : null;
      const latest = boundaryRef.current;
      if (latest.key !== key) return;
      latest.inFlight = false;
      if (loadedBucket != null && loadedBucket >= targetBucket) {
        latest.complete = true;
        return;
      }
      scheduleBoundaryRef.current?.(targetBucket, false);
    }, delay);
  }, [symbol, interval, loadData]);
  scheduleBoundaryRef.current = scheduleBoundaryRefresh;

  useEffect(() => {
    resetBoundaryRefresh();
    const request = loadData(true);
    return () => request?.controller.abort();
  }, [loadData, resetBoundaryRefresh]);

  useEffect(() => () => {
    resetBoundaryRefresh();
    requestRef.current.controller?.abort();
  }, [resetBoundaryRefresh]);

  useEffect(() => subscribeTicker(() => {
    const ticker = getTickerSnapshot();
    if (!ticker || ticker.symbol !== symbol) return;
    const bar = lastBarRef.current;
    const candle = seriesRef.current.candle;
    if (!bar || !candle) return;

    const price = Number(ticker.price);
    if (!Number.isFinite(price)) return;

    const bucket = intervalBucketStart(ticker.eventTime, interval);
    if (bucket != null && bucket > bar.time) {
      scheduleBoundaryRefresh(bucket, true);
      return;
    }
    if (bucket != null && bucket < bar.time) return;

    bar.close = price;
    if (price > bar.high) bar.high = price;
    if (price < bar.low) bar.low = price;
    try {
      candle.update(bar);
    } catch (_) {
      // Ignore chart updates during teardown.
    }
  }), [symbol, interval, scheduleBoundaryRefresh]);

  return (
    <div
      className="price-chart-root relative flex h-full min-w-0 flex-col overflow-hidden rounded-lg border border-border bg-card"
    >
      <div className="flex shrink-0 flex-nowrap items-center gap-3 border-b border-border px-3 py-2.5 sm:px-4">
        <div className="min-w-0 flex-1 overflow-x-auto">
          {headerLeading || <span className="whitespace-nowrap text-sm font-semibold text-white">{symbol}</span>}
        </div>
        <label className="sr-only" htmlFor="market-chart-interval">K线周期</label>
        <select
          id="market-chart-interval"
          aria-label="K线周期"
          value={interval}
          onChange={(event) => onIntervalChange(event.target.value)}
          className="h-8 shrink-0 border border-border bg-surface px-2 text-xs text-white outline-none focus:border-accent"
        >
          {INTERVALS.map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
        <button
          type="button"
          onClick={onOpenAssistant}
          aria-label={assistantOpen ? "收起 AI 行情分析" : "打开 AI 行情分析"}
          aria-pressed={assistantOpen}
          title="AI 行情分析"
          className={`flex h-8 w-8 shrink-0 items-center justify-center border bg-card transition-colors hover:border-accent/60 hover:text-accent ${assistantOpen ? "border-accent/60 text-accent" : "border-border text-muted"}`}
        >
          <MessageSquare size={15} />
        </button>
      </div>

      <div className="relative min-h-0 flex-1">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-card/60">
            <div className="text-sm text-muted">加载中...</div>
          </div>
        )}
        <div ref={containerRef} className="h-full w-full" />
      </div>
    </div>
  );
}
