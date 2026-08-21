import React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getTicker } from "../api/client";
import MarketSummary from "./MarketSummary";

let currentTicker;

vi.mock("../context/MarketTickerContext", () => ({ useTicker: () => currentTicker }));
vi.mock("../api/client", () => ({ getTicker: vi.fn() }));

describe("MarketSummary", () => {
  beforeEach(() => {
    currentTicker = null;
    getTicker.mockReset().mockResolvedValue({
      data: { symbol: "SOLUSDT", price: "140", high: "145", low: "135" },
    });
  });

  afterEach(cleanup);

  it("shows prices and the current interval indicator snapshot in one row", async () => {
    const view = render(
      <MarketSummary symbol="SOLUSDT" indicators={{ adx: 28.456, atr: 2.34567, rsi: 61.234 }} />,
    );
    await act(async () => Promise.resolve());

    expect(screen.getByText("当前价格").nextElementSibling.textContent).toContain("$140.00");
    expect(screen.getByText("24H高").nextElementSibling.textContent).toContain("$145.00");
    expect(screen.getByText("24H低").nextElementSibling.textContent).toContain("$135.00");
    expect(screen.getByText("ADX(14)").nextElementSibling.textContent).toContain("28.46");
    expect(screen.getByText("ATR(14)").nextElementSibling.textContent).toContain("2.3457");
    expect(screen.getByText("RSI(14)").nextElementSibling.textContent).toContain("61.23");
    expect(view.container.querySelector("section").className).toContain("flex-nowrap");
  });

  it("uses placeholders for absent or non-finite indicators", async () => {
    render(<MarketSummary symbol="SOLUSDT" indicators={{ adx: null, atr: Number.NaN, rsi: undefined }} />);
    await act(async () => Promise.resolve());
    expect(screen.getByText("ADX(14)").nextElementSibling.textContent).toBe("--");
    expect(screen.getByText("ATR(14)").nextElementSibling.textContent).toBe("--");
    expect(screen.getByText("RSI(14)").nextElementSibling.textContent).toBe("--");
  });

  it("prefers matching live ticker values over the REST fallback", async () => {
    currentTicker = { symbol: "SOLUSDT", price: "142.5", high: "147", low: "138" };
    render(<MarketSummary symbol="SOLUSDT" />);
    await act(async () => Promise.resolve());
    expect(screen.getByText("当前价格").nextElementSibling.textContent).toContain("$142.50");
    expect(screen.getByText("24H高").nextElementSibling.textContent).toContain("$147.00");
    expect(screen.getByText("24H低").nextElementSibling.textContent).toContain("$138.00");
  });

  it("reloads the REST snapshot when the global refresh revision changes", async () => {
    const view = render(<MarketSummary symbol="SOLUSDT" refreshRevision={0} />);
    await act(async () => Promise.resolve());
    expect(getTicker).toHaveBeenCalledTimes(1);
    view.rerender(<MarketSummary symbol="SOLUSDT" refreshRevision={1} />);
    await act(async () => Promise.resolve());
    expect(getTicker).toHaveBeenCalledTimes(2);
  });
});
