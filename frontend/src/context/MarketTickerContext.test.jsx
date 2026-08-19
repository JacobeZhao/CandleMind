import React from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearTicker,
  publishTicker,
  subscribeTicker,
  useTicker,
} from "./MarketTickerContext";

function TickerProbe() {
  const ticker = useTicker();
  return <span data-testid="ticker">{ticker ? `${ticker.symbol}:${ticker.price}:${ticker.high ?? "-"}` : "none"}</span>;
}

describe("market ticker store", () => {
  beforeEach(clearTicker);
  afterEach(() => {
    cleanup();
    clearTicker();
  });

  it("notifies subscribers and merges updates for the same symbol", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeTicker(listener);

    publishTicker({ symbol: "BTCUSDT", price: "100", high: "110" });
    publishTicker({ symbol: "BTCUSDT", price: "101" });

    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    publishTicker({ symbol: "BTCUSDT", price: "102" });
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("rerenders hook consumers when the snapshot changes or clears", () => {
    render(<TickerProbe />);
    expect(screen.getByTestId("ticker").textContent).toBe("none");

    act(() => publishTicker({ symbol: "SOLUSDT", price: "150", high: "155" }));
    expect(screen.getByTestId("ticker").textContent).toBe("SOLUSDT:150:155");

    act(clearTicker);
    expect(screen.getByTestId("ticker").textContent).toBe("none");
  });
});
