import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createChart } from "lightweight-charts";
import { getKlines } from "../api/client";
import PriceChart, { intervalBucketStart } from "./PriceChart";

let tickerSubscriber;
let currentTicker;
let resizeCallback;
let series;
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

vi.mock("../api/client", () => ({
  getKlines: vi.fn(),
}));

vi.mock("lightweight-charts", () => ({
  LineStyle: { Solid: 0 },
  createChart: vi.fn(),
}));

const bar = (openTime, close = 100, direction = 1) => ({
  open_time: openTime,
  open: 99,
  high: 101,
  low: 98,
  close,
  volume: 10,
  psar: direction === 1 ? 97 : 103,
  psar_direction: direction,
  adx14: 25,
  pdi: 30,
  ndi: 10,
});

function makeChart() {
  const createdSeries = [];
  const addSeries = (options = {}) => {
    const item = {
      options,
      setData: vi.fn(),
      update: vi.fn(),
      applyOptions: vi.fn(),
    };
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
        bar("2026-08-15T12:05:00.000Z", 102, -1),
      ],
    });
    global.ResizeObserver = class {
      constructor(callback) {
        resizeCallback = callback;
      }
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

  it("requests only fixed PSAR and ADX data and renders no indicator controls", async () => {
    render(<PriceChart symbol="SOLUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} headerLeading={<span>行情</span>} />);
    await act(async () => Promise.resolve());

    expect(getKlines).toHaveBeenCalledWith(
      "SOLUSDT",
      "5m",
      200,
      ["psar", "adx"],
      { psar: { step: 0.02, max: 0.2 }, adx: { period: 14 } },
      expect.any(AbortSignal),
    );
    expect(screen.queryByText("Parabolic SAR")).toBeNull();
    expect(screen.queryByText("ADX / DI")).toBeNull();
    expect(screen.queryByRole("button", { name: /指标/i })).toBeNull();

    const pointSeries = chart.createdSeries.filter((item) => item.options.lineVisible === false);
    expect(pointSeries).toHaveLength(2);
    expect(pointSeries[0].options.color).not.toBe(pointSeries[1].options.color);
    expect(pointSeries[0].setData).toHaveBeenCalledWith([
      expect.objectContaining({ value: 97 }),
    ]);
    expect(pointSeries[1].setData).toHaveBeenCalledWith([
      expect.objectContaining({ value: 103 }),
    ]);
  });

  it("uses an interval select and keeps the AI action rightmost", async () => {
    const onIntervalChange = vi.fn();
    const onOpenAssistant = vi.fn();
    const view = render(
      <PriceChart
        symbol="SOLUSDT"
        interval="5m"
        onIntervalChange={onIntervalChange}
        onOpenAssistant={onOpenAssistant}
      />,
    );
    await act(async () => Promise.resolve());

    const select = screen.getByRole("combobox", { name: "K线周期" });
    const aiButton = screen.getByRole("button", { name: "打开 AI 行情分析" });
    expect(select.value).toBe("5m");
    expect(select.compareDocumentPosition(aiButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.change(select, { target: { value: "1h" } });
    expect(onIntervalChange).toHaveBeenCalledWith("1h");

    fireEvent.click(aiButton);
    expect(onOpenAssistant).toHaveBeenCalledTimes(1);
    view.unmount();
  });

  it("reflects the controlled assistant state without recreating the chart", async () => {
    const props = {
      symbol: "SOLUSDT",
      interval: "5m",
      onIntervalChange: vi.fn(),
      onOpenAssistant: vi.fn(),
    };
    const view = render(<PriceChart {...props} assistantOpen={false} />);
    await act(async () => Promise.resolve());
    expect(screen.getByRole("button", { name: "打开 AI 行情分析" }).getAttribute("aria-pressed")).toBe("false");

    view.rerender(<PriceChart {...props} assistantOpen />);
    expect(screen.getByRole("button", { name: "收起 AI 行情分析" }).getAttribute("aria-pressed")).toBe("true");
    expect(createChart).toHaveBeenCalledTimes(1);
  });

  it("silently refreshes a closed-candle boundary and preserves the viewport", async () => {
    getKlines
      .mockResolvedValueOnce({ data: [bar("2026-08-15T12:00:00.000Z")] })
      .mockResolvedValueOnce({ data: [
        bar("2026-08-15T12:00:00.000Z"),
        bar("2026-08-15T12:05:00.000Z", 102),
      ] });
    render(<PriceChart symbol="BTCUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    expect(screen.queryByText("加载中...")).toBeNull();

    currentTicker = {
      symbol: "BTCUSDT",
      price: "102",
      eventTime: Date.parse("2026-08-15T12:05:00.000Z"),
    };
    act(() => tickerSubscriber());
    await act(async () => vi.advanceTimersByTime(250));

    expect(getKlines).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("加载中...")).toBeNull();
    expect(chart.timeScaleApi.getVisibleLogicalRange).toHaveBeenCalledTimes(1);
    expect(chart.timeScaleApi.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 12, to: 42 });
  });

  it("updates the live candle without a React ticker render", async () => {
    render(<PriceChart symbol="BTCUSDT" interval="5m" onIntervalChange={vi.fn()} onOpenAssistant={vi.fn()} />);
    await act(async () => Promise.resolve());
    const candleSeries = chart.createdSeries[0];

    currentTicker = {
      symbol: "BTCUSDT",
      price: "103.5",
      eventTime: Date.parse("2026-08-15T12:05:30.000Z"),
    };
    act(() => tickerSubscriber());
    expect(candleSeries.update).toHaveBeenCalledWith(expect.objectContaining({
      close: 103.5,
      high: 103.5,
    }));
  });

  it("ignores ResizeObserver notifications until dimensions change", async () => {
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
    expect(chart.applyOptions).toHaveBeenCalledTimes(1);
    expect(chart.applyOptions).toHaveBeenCalledWith({ width: 640, height: 480 });

    resizeCallback();
    await act(async () => vi.advanceTimersByTime(16));
    expect(chart.applyOptions).toHaveBeenCalledTimes(1);
  });
});
