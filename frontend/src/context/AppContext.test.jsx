import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppProvider, useApp } from "./AppContext";
import { getSettings, saveSettings } from "../api/client";

let receiveMessage;

vi.mock("../hooks/useWebSocket", () => ({
  useWebSocket: (onMessage) => {
    receiveMessage = onMessage;
  },
}));

vi.mock("../api/client", () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
}));

function Probe() {
  const { ticker, symbol, setSymbol } = useApp();
  return (
    <div>
      <span data-testid="symbol">{symbol}</span>
      <span data-testid="price">{ticker?.price ?? "none"}</span>
      <button onClick={() => setSymbol("SOLUSDT")}>switch</button>
    </div>
  );
}

describe("AppProvider ticker scheduling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getSettings.mockResolvedValue({ data: { symbol: "BTCUSDT", testnet: true } });
    saveSettings.mockResolvedValue({ data: { symbol: "SOLUSDT" } });
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("commits only the latest ticker once per 500ms window", async () => {
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    act(() => {
      receiveMessage({ type: "ticker", data: { symbol: "BTCUSDT", price: "100", high: "110" } });
      receiveMessage({ type: "ticker", data: { symbol: "BTCUSDT", price: "102", low: "90" } });
      vi.advanceTimersByTime(499);
    });
    expect(screen.getByTestId("price").textContent).toBe("none");

    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByTestId("price").textContent).toBe("102");
  });

  it("drops pending and old-symbol events while switching symbols", async () => {
    let finishSave;
    saveSettings.mockReturnValue(new Promise((resolve) => { finishSave = resolve; }));
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    act(() => receiveMessage({ type: "ticker", data: { symbol: "BTCUSDT", price: "100" } }));
    fireEvent.click(screen.getByText("switch"));
    act(() => {
      receiveMessage({ type: "ticker", data: { symbol: "BTCUSDT", price: "101" } });
      vi.advanceTimersByTime(500);
    });
    expect(screen.getByTestId("price").textContent).toBe("none");

    await act(async () => finishSave({ data: { symbol: "SOLUSDT" } }));
    act(() => {
      receiveMessage({ type: "ticker", data: { symbol: "BTCUSDT", price: "103" } });
      receiveMessage({ type: "ticker", data: { symbol: "SOLUSDT", price: "150" } });
      vi.advanceTimersByTime(500);
    });
    expect(screen.getByTestId("symbol").textContent).toBe("SOLUSDT");
    expect(screen.getByTestId("price").textContent).toBe("150");
  });
});
