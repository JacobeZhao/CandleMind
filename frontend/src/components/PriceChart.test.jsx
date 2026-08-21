import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createChart } from "lightweight-charts";
import { getKlines } from "../api/client";
import PriceChart, { intervalBucketStart } from "./PriceChart";

let tickerSubscriber;
let currentTicker;
let resizeCallback;
let chart;

vi.mock("../context/MarketTickerContext", () => ({
  getTickerSnapshot: vi.fn(() => currentTicker),
  subscribeTicker: vi.fn((callback) => {
    tickerSubscriber = callback;
    return () => {
      if (tickerSubscriber === callback) tickerSubscriber = undefined;
    };
  }),
}));

vi.mock("../api/client", () => ({ getKlines: vi.fn() }));
vi.mock("lightweight-charts", () => ({ LineStyle: { Solid: 0 }, createChart: vi.fn() }));

const bar = (openTime, overrides = {}) => ({
  open_time: openTime,
  open: 99,
  high: 101,
  low: 98,
  close: 100,
  volume: 10,
  psar: 97,
  psar_direction: 1,
  adx14: 25.12,
  atr14: 2.34,
  rsi14: 58.76,
  ema20: 99.5,
  ema100: 96.5,
  supertrend: 95,
  supertrend_direction: 1,
  bb_upper: 104,
  bb_middle: 100,
  bb_lower: 96,
  ...overrides,
});

function makeChart() {
  const createdSeries = [];
  const addSeries = (options = {}) => {
    const item = { options, setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() };
    createdSeries.push(item);
    return item;
  };
  const timeScale = {
    getVisibleLogicalRange: vi.fn(() => ({ from: 12, to: 42 })),
    setVisibleLogicalRange: vi.fn(),
  };
  return {
    createdSeries,
    addCandlestickSeries: vi.fn(addSeries),
    addHistogramSeries: vi.fn(addSeries),
    addLineSeries: vi.fn(addSeries),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
    timeScale: vi.fn(() => timeScale),
    timeScaleApi: timeScale,
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

describe("PriceChart", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    tickerSubscriber = undefined;
    currentTicker = null;
    resizeCallback = undefined;
    chart = makeChart();
    createChart.mockReset().mockReturnValue(chart);
    getKlines.mockReset().mockResolvedValue({
      data: [
        bar("2026-08-15T12:00:00.000Z"),
        bar("2026-08-15T12:05:00.000Z", { close: 102, psar: 103, psar_direction: -1 }),
      ],
    });
    global.ResizeObserver = class {
      constructor(callback) { resizeCallback = callback; }
      observe() {}
      disconnect() {}
    };
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("computes exchange-time buckets for supported intervals", () => {
    const eventTime = Date.parse("2026-08-15T12:07:12.000Z");
    expect(intervalBucketStart(eventTime, "5m")).toBe(Date.parse("2026-08-15T12:05:00.000Z") / 1000);
    expect(intervalBucketStart(eventTime, "1h")).toBe(Date.parse("2026-08-15T12:00:00.000Z") / 1000);
    expect(intervalBucketStart(eventTime, "bad")).toBeNull();
  });

  it("defaults to SAR, requests summary indicators, and reports the latest snapshot", async () => {
    const onIndicatorSnapshot = vi.fn();
    render(
      <PriceChart
        symbol="SOLUSDT"
        interval="5m"
        onIntervalChange={vi.fn()}
        onOpenAssistant={vi.fn()}
        onIndicatorSnapshot={onIndicatorSnapshot}
      />,
    );
    await act(async () => Promise.resolve());

    expect(screen.getByRole("combobox", { name: "主图指标" }).value).toBe("psar");
    expect(getKlines).toHaveBeenCalledWith(
      "SOLUSDT",
      "5m",
      200,
      ["adx", "atr", "rsi", "psar"],
      {
        adx: { period: 14 },
        atr: { period: 14 },
        rsi: { period: 14 },
        psar: { step: 0.02, max: 0.2 },
      },
      expect.any(AbortSignal),
    );
    expect(onIndicatorSnapshot).toHaveBeenLastCalledWith({ adx: 25.12, atr: 2.34, rsi: 58.76 });

    const pointSeries = chart.createdSeries.filter((item) => item.options.lineVisible === false);
    expect(pointSeries).toHaveLength(2);
    expect(pointSeries[0].options.color).not.toBe(pointSeries[1].options.color);
    expect(pointSeries[0].setData).toHaveBeenLastCalledWith([expect.objectContaining({ value: 97 })]);
    expect(pointSeries[1].setData).toHaveBeenLastCalledWith([expect.objectContaining({ value: 103 })]);
  });

  it("offers five main indicators before the interval and uses a text AI action", async () => {
    const onIntervalChange = vi.fn();
    const onOpenAssistant = vi.fn();
    render(
      <PriceChart
        symbol="SOLUSDT"
        interval="5m"
        onIntervalChange={onIntervalChange}
        onOpenAssistant={onOpenAssistant}
      />,
    );
    await act(async () => Promise.resolve());

    const indicator = screen.getByRole("combobox", { name: "主图指标" });
    const interval = screen.getByRole("combobox", { name: "K线周期" });
    const aiButton = screen.getByRole("button", { name: "打开 AI 行情分析" });
    expect([...indicator.options].map((option) => option.textContent)).toEqual([
      "SAR", "EMA20", "EMA100", "超级趋势", "布林带",
    ]);
    expect(indicator.compareDocumentPosition(interval) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(interval.compareDocumentPosition(aiButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(aiButton.textContent).toBe("AI行情分析");

    fireEvent.change(interval, { target: { value: "1h" } });
    expect(onIntervalChange).toHaveBeenCalledWith("1h");
    fireEvent.click(aiButton);
    expect(onOpenAssistant).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["ema20", "ema", { period: 20 }],
    ["ema100", "ema", { period: 100 }],
    ["supertrend", "supertrend", { period: 10, multiplier: 3 }],
    ["bb", "bb", { period: 20, std_dev: 2 }],
  ])("requests the selected %s overlay without recreating the chart", async (value, requestId, params) => {
    render(<PriceChart symbol="SOLUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    fireEvent.change(screen.getByRole("combobox", { name: "主图指标" }), { target: { value } });
    await act(async () => Promise.resolve());

    expect(getKlines).toHaveBeenLastCalledWith(
      "SOLUSDT",
      "5m",
      200,
      ["adx", "atr", "rsi", requestId],
      expect.objectContaining({ [requestId]: params }),
      expect.any(AbortSignal),
    );
    expect(createChart).toHaveBeenCalledTimes(1);
  });

  it("removes the ADX/DI sub-chart, keeps volume, and breaks Supertrend at reversals", async () => {
    getKlines.mockResolvedValue({
      data: [
        bar("2026-08-15T12:00:00.000Z", { supertrend: 95, supertrend_direction: 1 }),
        bar("2026-08-15T12:05:00.000Z", { supertrend: 105, supertrend_direction: -1 }),
        bar("2026-08-15T12:10:00.000Z", { supertrend: 96, supertrend_direction: 1 }),
      ],
    });
    render(<PriceChart symbol="SOLUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    fireEvent.change(screen.getByRole("combobox", { name: "主图指标" }), { target: { value: "supertrend" } });
    await act(async () => Promise.resolve());

    expect(chart.addHistogramSeries).toHaveBeenCalledTimes(1);
    expect(chart.priceScale).not.toHaveBeenCalledWith("sub");
    const bullSeries = chart.createdSeries[5];
    const bearSeries = chart.createdSeries[6];
    expect(bullSeries.setData).toHaveBeenLastCalledWith([
      expect.objectContaining({ value: 95 }),
      expect.not.objectContaining({ value: expect.anything() }),
      expect.objectContaining({ value: 96 }),
    ]);
    expect(bearSeries.setData).toHaveBeenLastCalledWith([
      expect.not.objectContaining({ value: expect.anything() }),
      expect.objectContaining({ value: 105 }),
      expect.not.objectContaining({ value: expect.anything() }),
    ]);
  });

  it("ignores a stale response after the indicator changes", async () => {
    const oldRequest = deferred();
    const newRequest = deferred();
    getKlines.mockReset().mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(newRequest.promise);
    const onIndicatorSnapshot = vi.fn();
    render(
      <PriceChart
        symbol="SOLUSDT"
        interval="5m"
        onIntervalChange={vi.fn()}
        onOpenAssistant={vi.fn()}
        onIndicatorSnapshot={onIndicatorSnapshot}
      />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: "主图指标" }), { target: { value: "ema20" } });
    await act(async () => {
      newRequest.resolve({ data: [bar("2026-08-15T12:05:00.000Z", { adx14: 31, ema20: 101 })] });
      await Promise.resolve();
    });
    await act(async () => {
      oldRequest.resolve({ data: [bar("2026-08-15T12:00:00.000Z", { adx14: 9 })] });
      await Promise.resolve();
    });

    expect(onIndicatorSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ adx: 31 }));
    expect(chart.createdSeries[4].setData).toHaveBeenLastCalledWith([expect.objectContaining({ value: 101 })]);
  });

  it("silently refreshes a closed-candle boundary and preserves the viewport", async () => {
    getKlines
      .mockResolvedValueOnce({ data: [bar("2026-08-15T12:00:00.000Z")] })
      .mockResolvedValueOnce({ data: [
        bar("2026-08-15T12:00:00.000Z"),
        bar("2026-08-15T12:05:00.000Z", { close: 102 }),
      ] });
    render(<PriceChart symbol="BTCUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    currentTicker = { symbol: "BTCUSDT", price: "102", eventTime: Date.parse("2026-08-15T12:05:00.000Z") };
    act(() => tickerSubscriber());
    await act(async () => vi.advanceTimersByTime(250));

    expect(getKlines).toHaveBeenCalledTimes(2);
    expect(chart.timeScaleApi.getVisibleLogicalRange).toHaveBeenCalledTimes(1);
    expect(chart.timeScaleApi.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 12, to: 42 });
  });

  it("updates only the live candle on an intra-bucket ticker event", async () => {
    render(<PriceChart symbol="BTCUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    currentTicker = { symbol: "BTCUSDT", price: "103.5", eventTime: Date.parse("2026-08-15T12:05:30.000Z") };
    act(() => tickerSubscriber());
    expect(chart.createdSeries[0].update).toHaveBeenCalledWith(expect.objectContaining({ close: 103.5, high: 103.5 }));
    expect(getKlines).toHaveBeenCalledTimes(1);
  });

  it("does not resize the chart until dimensions change", async () => {
    const view = render(<PriceChart symbol="SOLUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    const container = view.container.querySelector(".price-chart-root > div:last-of-type > div:last-child");
    resizeCallback();
    await act(async () => vi.advanceTimersByTime(16));
    expect(chart.applyOptions).not.toHaveBeenCalled();
    Object.defineProperty(container, "clientWidth", { configurable: true, value: 640 });
    Object.defineProperty(container, "clientHeight", { configurable: true, value: 480 });
    resizeCallback();
    await act(async () => vi.advanceTimersByTime(16));
    expect(chart.applyOptions).toHaveBeenCalledWith({ width: 640, height: 480 });
  });
});
