import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppProvider, useApp } from "./AppContext";
import { clearTicker, useTicker } from "./MarketTickerContext";
import { getEngineStatus, getSettings, saveSettings } from "../api/client";

let receiveMessage;

vi.mock("../hooks/useWebSocket", () => ({
  useWebSocket: (onMessage) => {
    receiveMessage = onMessage;
  },
}));

vi.mock("../api/client", () => ({
  getSettings: vi.fn(),
  getEngineStatus: vi.fn(),
  saveSettings: vi.fn(),
}));

function Probe() {
  const { symbol, setSymbol, botStatus, botStatusLoaded, networkTab, networkSwitching, networkError, switchNetwork } = useApp();
  const ticker = useTicker();
  return (
    <div>
      <span data-testid="symbol">{symbol}</span>
      <span data-testid="price">{ticker?.price ?? "none"}</span>
      <span data-testid="bot-loaded">{String(botStatusLoaded)}</span>
      <span data-testid="bot-state">{botStatus?.engine_state ?? "none"}</span>
      <span data-testid="network">{networkTab}</span>
      <span data-testid="network-switching">{String(networkSwitching)}</span>
      <span data-testid="network-error">{networkError ?? "none"}</span>
      <button onClick={() => setSymbol("SOLUSDT")}>switch</button>
      <button onClick={() => switchNetwork("main")}>mainnet</button>
    </div>
  );
}

describe("AppProvider ticker scheduling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    clearTicker();
    getSettings.mockResolvedValue({ data: { symbol: "BTCUSDT", testnet: true } });
    getEngineStatus.mockResolvedValue({ data: { engine_state: "stopped", running: false } });
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

  it("does not rerender non-ticker context consumers when ticker updates", async () => {
    let appRenderCount = 0;
    let tickerRenderCount = 0;

    function AppStateProbe() {
      useApp();
      appRenderCount += 1;
      return <span data-testid="app-renders">{appRenderCount}</span>;
    }

    function TickerProbe() {
      const ticker = useTicker();
      tickerRenderCount += 1;
      return <span data-testid="ticker-renders">{tickerRenderCount}:{ticker?.price ?? "none"}</span>;
    }

    render(<AppProvider><AppStateProbe /><TickerProbe /></AppProvider>);
    await act(async () => Promise.resolve());
    const appRendersBeforeTicker = appRenderCount;
    const tickerRendersBeforeTicker = tickerRenderCount;

    act(() => {
      receiveMessage({ type: "ticker", data: { symbol: "BTCUSDT", price: "101" } });
      vi.advanceTimersByTime(500);
    });

    expect(appRenderCount).toBe(appRendersBeforeTicker);
    expect(tickerRenderCount).toBe(tickerRendersBeforeTicker + 1);
    expect(screen.getByTestId("ticker-renders").textContent).toContain(":101");
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

  it("loads engine status immediately without inventing a position or fill count", async () => {
    let resolveStatus;
    getEngineStatus.mockReturnValue(new Promise((resolve) => { resolveStatus = resolve; }));

    render(<AppProvider><Probe /></AppProvider>);

    expect(screen.getByTestId("bot-loaded").textContent).toBe("false");
    expect(screen.getByTestId("bot-state").textContent).toBe("none");
    expect(getEngineStatus).toHaveBeenCalledOnce();

    await act(async () => resolveStatus({ data: { engine_state: "running", running: true } }));
    expect(screen.getByTestId("bot-loaded").textContent).toBe("true");
    expect(screen.getByTestId("bot-state").textContent).toBe("running");
  });

  it("does not let a late REST response overwrite a newer WebSocket status", async () => {
    let resolveStatus;
    getEngineStatus.mockReturnValue(new Promise((resolve) => { resolveStatus = resolve; }));
    render(<AppProvider><Probe /></AppProvider>);

    act(() => receiveMessage({
      type: "bot_status",
      data: { engine_state: "retrying", running: true, failure_count: 1 },
    }));
    expect(screen.getByTestId("bot-state").textContent).toBe("retrying");

    await act(async () => resolveStatus({ data: { engine_state: "stopped", running: false } }));
    expect(screen.getByTestId("bot-state").textContent).toBe("retrying");
  });

  it("retries the initial engine status request after a temporary failure", async () => {
    getEngineStatus
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ data: { engine_state: "stopped", running: false } });

    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());
    expect(screen.getByTestId("bot-loaded").textContent).toBe("false");

    await act(async () => {
      vi.advanceTimersByTime(3000);
      await Promise.resolve();
    });

    expect(getEngineStatus).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("bot-loaded").textContent).toBe("true");
  });

  it("uses the backend response as the authority when switching networks", async () => {
    saveSettings.mockResolvedValue({ data: { testnet: false } });
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    await act(async () => fireEvent.click(screen.getByText("mainnet")));

    expect(saveSettings).toHaveBeenCalledWith({ testnet: false });
    expect(screen.getByTestId("network").textContent).toBe("main");
    expect(screen.getByTestId("network-switching").textContent).toBe("false");
  });

  it("keeps the current network and exposes a switch failure", async () => {
    saveSettings.mockRejectedValue({ response: { data: { detail: "目标网络行情连接超时" } } });
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    await act(async () => fireEvent.click(screen.getByText("mainnet")));

    expect(screen.getByTestId("network").textContent).toBe("test");
    expect(screen.getByTestId("network-error").textContent).toBe("目标网络行情连接超时");
  });

  it("deduplicates concurrent network switch requests", async () => {
    let finishSwitch;
    saveSettings.mockReturnValue(new Promise((resolve) => { finishSwitch = resolve; }));
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    fireEvent.click(screen.getByText("mainnet"));
    fireEvent.click(screen.getByText("mainnet"));
    expect(saveSettings).toHaveBeenCalledTimes(1);

    await act(async () => finishSwitch({ data: { testnet: false } }));
    expect(screen.getByTestId("network").textContent).toBe("main");
  });
});
