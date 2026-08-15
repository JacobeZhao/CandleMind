import React from "react";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PriceChart, { intervalBucketStart } from "./PriceChart";
import { getKlines } from "../api/client";

let currentTicker = null;
const candleSeries = { setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() };
const genericSeries = () => ({ setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() });

vi.mock("../context/AppContext", () => ({
  useApp: () => ({ ticker: currentTicker }),
}));

vi.mock("../api/client", () => ({
  getKlines: vi.fn(),
}));

vi.mock("lightweight-charts", () => ({
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  createChart: vi.fn(() => ({
    addCandlestickSeries: () => candleSeries,
    addHistogramSeries: genericSeries,
    addLineSeries: genericSeries,
    priceScale: () => ({ applyOptions: vi.fn() }),
    timeScale: () => ({ setVisibleLogicalRange: vi.fn() }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  })),
}));

vi.mock("./MarketAiDialog", () => ({ default: () => null }));

const bar = (openTime, close = 100) => ({
  open_time: openTime,
  open: 99,
  high: 101,
  low: 98,
  close,
  volume: 10,
  psar: 97,
  psar_direction: 1,
  adx14: 25,
  pdi: 30,
  ndi: 10,
});

describe("PriceChart market boundaries", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    currentTicker = null;
    candleSeries.setData.mockClear();
    candleSeries.update.mockClear();
    getKlines.mockReset();
    global.ResizeObserver = class {
      observe() {}
      disconnect() {}
    };
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("computes exchange-time buckets for supported intervals", () => {
    const eventTime = Date.parse("2026-08-15T12:07:12.000Z");
    expect(intervalBucketStart(eventTime, "5m")).toBe(Date.parse("2026-08-15T12:05:00.000Z") / 1000);
    expect(intervalBucketStart(eventTime, "1h")).toBe(Date.parse("2026-08-15T12:00:00.000Z") / 1000);
    expect(intervalBucketStart(eventTime, "bad")).toBeNull();
  });

  it("retries a stale boundary response until the target bar is loaded", async () => {
    getKlines
      .mockResolvedValueOnce({ data: [bar("2026-08-15T12:00:00.000Z")] })
      .mockResolvedValueOnce({ data: [bar("2026-08-15T12:00:00.000Z", 101)] })
      .mockResolvedValueOnce({ data: [
        bar("2026-08-15T12:00:00.000Z"),
        bar("2026-08-15T12:05:00.000Z", 102),
      ] });

    const view = render(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    await act(async () => Promise.resolve());

    currentTicker = {
      symbol: "BTCUSDT",
      price: "101.5",
      eventTime: Date.parse("2026-08-15T12:04:59.000Z"),
    };
    view.rerender(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    expect(candleSeries.update).toHaveBeenCalledWith(expect.objectContaining({ close: 101.5 }));

    currentTicker = {
      symbol: "BTCUSDT",
      price: "102",
      eventTime: Date.parse("2026-08-15T12:05:00.000Z"),
    };
    view.rerender(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    expect(getKlines).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(250));
    expect(getKlines).toHaveBeenCalledTimes(2);
    expect(candleSeries.setData).toHaveBeenLastCalledWith([
      expect.objectContaining({ time: Date.parse("2026-08-15T12:00:00.000Z") / 1000 }),
    ]);

    await act(async () => vi.advanceTimersByTime(499));
    expect(getKlines).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTime(1));
    expect(getKlines).toHaveBeenCalledTimes(3);

    currentTicker = { ...currentTicker, price: "102.5" };
    view.rerender(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    await act(async () => Promise.resolve());
    expect(getKlines).toHaveBeenCalledTimes(3);
    expect(candleSeries.setData).toHaveBeenLastCalledWith(expect.arrayContaining([
      expect.objectContaining({ time: Date.parse("2026-08-15T12:05:00.000Z") / 1000 }),
    ]));

    await act(async () => vi.advanceTimersByTime(5_000));
    expect(getKlines).toHaveBeenCalledTimes(3);

    const latestSignal = getKlines.mock.calls.at(-1)[5];
    view.unmount();
    expect(latestSignal.aborted).toBe(true);
  });

  it("cancels pending boundary work when interval or symbol changes", async () => {
    getKlines.mockResolvedValue({ data: [bar("2026-08-15T12:00:00.000Z")] });
    const view = render(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    await act(async () => Promise.resolve());

    currentTicker = {
      symbol: "BTCUSDT",
      price: "102",
      eventTime: Date.parse("2026-08-15T12:05:00.000Z"),
    };
    view.rerender(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    view.getByText("15m").click();
    await act(async () => Promise.resolve());
    expect(getKlines).toHaveBeenCalledTimes(2);

    currentTicker = {
      symbol: "BTCUSDT",
      price: "103",
      eventTime: Date.parse("2026-08-15T12:15:00.000Z"),
    };
    view.rerender(<PriceChart symbol="BTCUSDT" defaultInterval="5m" />);
    view.rerender(<PriceChart symbol="SOLUSDT" defaultInterval="5m" />);
    await act(async () => Promise.resolve());
    expect(getKlines).toHaveBeenCalledTimes(3);

    await act(async () => vi.advanceTimersByTime(2_000));
    expect(getKlines).toHaveBeenCalledTimes(3);
    expect(getKlines.mock.calls[1][5].aborted).toBe(true);
  });
});
