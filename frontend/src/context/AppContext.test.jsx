import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppProvider, useApp } from "./AppContext";
import { clearTicker, useTicker } from "./MarketTickerContext";
import { getAccountBalance, getEngineStatus, getSettings, getStrategyConfig, saveSettings, startEngine } from "../api/client";

let receiveMessage;

vi.mock("../hooks/useWebSocket", () => ({
  useWebSocket: (onMessage) => {
    receiveMessage = onMessage;
  },
}));

vi.mock("../api/client", () => ({
  getSettings: vi.fn(),
  getAccountBalance: vi.fn(),
  getEngineStatus: vi.fn(),
  getStrategyConfig: vi.fn(),
  saveSettings: vi.fn(),
  startEngine: vi.fn(),
  stopEngine: vi.fn(),
}));

function Probe() {
  const { account, accountError, symbol, symbolSwitching, setSymbol, botStatus, botStatusLoaded, networkTab, networkSwitching, networkError, strategyCommandPending, strategyCommandError, strategyStatusUncertain, refreshRevision, refreshPending, refreshError, refreshAll, startStrategy, switchNetwork } = useApp();
  const ticker = useTicker();
  return (
    <div>
      <span data-testid="symbol">{symbol}</span>
      <span data-testid="price">{ticker?.price ?? "none"}</span>
      <span data-testid="bot-loaded">{String(botStatusLoaded)}</span>
      <span data-testid="bot-state">{botStatus?.engine_state ?? "none"}</span>
      <span data-testid="network">{networkTab}</span>
      <span data-testid="network-switching">{String(networkSwitching)}</span>
      <span data-testid="symbol-switching">{String(symbolSwitching)}</span>
      <span data-testid="network-error">{networkError ?? "none"}</span>
      <span data-testid="account-balance">{account?.totalWalletBalance ?? "none"}</span>
      <span data-testid="account-error">{accountError ?? "none"}</span>
      <span data-testid="command-pending">{String(strategyCommandPending)}</span>
      <span data-testid="command-error">{strategyCommandError ?? "none"}</span>
      <span data-testid="status-uncertain">{String(strategyStatusUncertain)}</span>
      <span data-testid="refresh-revision">{refreshRevision}</span>
      <span data-testid="refresh-pending">{String(refreshPending)}</span>
      <span data-testid="refresh-error">{refreshError ?? "none"}</span>
      <button onClick={() => setSymbol("SOLUSDT")}>switch</button>
      <button onClick={() => switchNetwork("main")}>mainnet</button>
      <button onClick={() => { startStrategy(); startStrategy(); }}>start-twice</button>
      <button onClick={() => { refreshAll(); refreshAll(); }}>refresh-twice</button>
    </div>
  );
}

describe("AppProvider ticker scheduling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    clearTicker();
    getSettings.mockResolvedValue({ data: { symbol: "BTCUSDT", testnet: true } });
    getAccountBalance.mockResolvedValue({ data: { totalWalletBalance: "100.00" } });
    getEngineStatus.mockResolvedValue({ data: { engine_state: "stopped", running: false } });
    getStrategyConfig.mockResolvedValue({
      data: {
        strategy_type: "sar_adx_trend",
        config_version: "sar_adx_trend_v1",
        config_hash: "trend-config-hash",
        parameters: { execution_interval: "5m", sar_step: 0.02 },
      },
    });
    saveSettings.mockResolvedValue({ data: { symbol: "SOLUSDT" } });
    startEngine.mockResolvedValue({ data: { message: "started" } });
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

  it("loads the active account immediately on startup", async () => {
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    expect(getAccountBalance).toHaveBeenCalledOnce();
    expect(screen.getByTestId("account-balance").textContent).toBe("100.00");
    expect(screen.getByTestId("account-error").textContent).toBe("none");
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
    saveSettings.mockResolvedValue({
      data: { testnet: false, account: { totalWalletBalance: "321.00" } },
    });
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    await act(async () => fireEvent.click(screen.getByText("mainnet")));

    expect(saveSettings).toHaveBeenCalledWith({ testnet: false });
    expect(screen.getByTestId("network").textContent).toBe("main");
    expect(screen.getByTestId("account-balance").textContent).toBe("321.00");
    expect(screen.getByTestId("account-error").textContent).toBe("none");
    expect(screen.getByTestId("network-switching").textContent).toBe("false");
  });

  it("does not let a late settings response overwrite a completed network switch", async () => {
    let finishSettings;
    getSettings.mockReturnValue(new Promise((resolve) => { finishSettings = resolve; }));
    saveSettings.mockResolvedValue({ data: { testnet: false } });
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    await act(async () => fireEvent.click(screen.getByText("mainnet")));
    expect(screen.getByTestId("network").textContent).toBe("main");

    await act(async () => finishSettings({ data: { symbol: "BTCUSDT", testnet: true } }));
    expect(screen.getByTestId("network").textContent).toBe("main");
  });

  it("clears stale account data when a private account poll fails", async () => {
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    act(() => receiveMessage({
      type: "account",
      data: { totalWalletBalance: "999.00" },
    }));
    expect(screen.getByTestId("account-balance").textContent).toBe("999.00");

    act(() => receiveMessage({
      type: "account_error",
      data: { message: "账户鉴权失败" },
    }));

    expect(screen.getByTestId("account-balance").textContent).toBe("none");
    expect(screen.getByTestId("account-error").textContent).toBe("账户鉴权失败");
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

  it("deduplicates strategy commands issued in the same tick", async () => {
    let finishStart;
    startEngine.mockReturnValue(new Promise((resolve) => { finishStart = resolve; }));
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    fireEvent.click(screen.getByText("start-twice"));

    expect(startEngine).toHaveBeenCalledOnce();
    expect(startEngine).toHaveBeenCalledWith(expect.objectContaining({
      symbol: "BTCUSDT",
      capital_limit: 1000,
      strategy_type: "sar_adx_trend",
      config_version: "sar_adx_trend_v1",
      config_hash: "trend-config-hash",
    }));
    expect(screen.getByTestId("command-pending").textContent).toBe("true");

    await act(async () => finishStart({ data: { message: "started" } }));
    expect(screen.getByTestId("command-pending").textContent).toBe("false");
  });

  it("blocks strategy start when the authoritative configuration cannot be loaded", async () => {
    getStrategyConfig.mockRejectedValueOnce(new Error("configuration unavailable"));
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    fireEvent.click(screen.getByText("start-twice"));

    expect(startEngine).not.toHaveBeenCalled();
    expect(screen.getByTestId("command-error").textContent).toContain("尚未加载");
  });

  it("blocks strategy start until a symbol switch has completed", async () => {
    let finishSave;
    saveSettings.mockReturnValue(new Promise((resolve) => { finishSave = resolve; }));
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    fireEvent.click(screen.getByText("switch"));
    fireEvent.click(screen.getByText("start-twice"));

    expect(screen.getByTestId("symbol-switching").textContent).toBe("true");
    expect(startEngine).not.toHaveBeenCalled();

    await act(async () => finishSave({ data: { symbol: "SOLUSDT" } }));
    expect(screen.getByTestId("symbol-switching").textContent).toBe("false");
  });

  it("reports status refresh failure without claiming the accepted command failed", async () => {
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());
    getEngineStatus.mockRejectedValueOnce(new Error("status unavailable"));

    await act(async () => fireEvent.click(screen.getByText("start-twice")));

    expect(startEngine).toHaveBeenCalledOnce();
    expect(screen.getByTestId("command-error").textContent).toContain("命令已提交");
    expect(screen.getByTestId("command-error").textContent).not.toContain("操作失败");
    expect(screen.getByTestId("status-uncertain").textContent).toBe("true");

    fireEvent.click(screen.getByText("start-twice"));
    expect(startEngine).toHaveBeenCalledOnce();
  });

  it("keeps a newer WebSocket status authoritative when command status refresh fails", async () => {
    let rejectStatusRefresh;
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());
    getEngineStatus.mockReturnValueOnce(new Promise((resolve, reject) => {
      rejectStatusRefresh = reject;
    }));

    fireEvent.click(screen.getByText("start-twice"));
    await act(async () => Promise.resolve());
    act(() => receiveMessage({
      type: "bot_status",
      data: { engine_state: "running", running: true },
    }));
    await act(async () => rejectStatusRefresh(new Error("late REST failure")));

    expect(screen.getByTestId("bot-state").textContent).toBe("running");
    expect(screen.getByTestId("status-uncertain").textContent).toBe("false");
    expect(screen.getByTestId("command-error").textContent).toBe("none");
  });

  it("deduplicates concurrent refreshes and increments the page revision once", async () => {
    let finishAccountRefresh;
    let finishStatusRefresh;
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    getAccountBalance.mockReturnValueOnce(new Promise((resolve) => { finishAccountRefresh = resolve; }));
    getEngineStatus.mockReturnValueOnce(new Promise((resolve) => { finishStatusRefresh = resolve; }));
    fireEvent.click(screen.getByText("refresh-twice"));

    expect(getAccountBalance).toHaveBeenCalledTimes(2);
    expect(getEngineStatus).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("refresh-pending").textContent).toBe("true");

    await act(async () => {
      finishAccountRefresh({ data: { totalWalletBalance: "200.00" } });
      finishStatusRefresh({ data: { engine_state: "running", running: true } });
    });

    expect(screen.getByTestId("account-balance").textContent).toBe("200.00");
    expect(screen.getByTestId("bot-state").textContent).toBe("running");
    expect(screen.getByTestId("refresh-revision").textContent).toBe("1");
    expect(screen.getByTestId("refresh-pending").textContent).toBe("false");
  });

  it("blocks network and strategy commands while a refresh is running", async () => {
    let finishAccountRefresh;
    let finishStatusRefresh;
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    getAccountBalance.mockReturnValueOnce(new Promise((resolve) => {
      finishAccountRefresh = resolve;
    }));
    getEngineStatus.mockReturnValueOnce(new Promise((resolve) => {
      finishStatusRefresh = resolve;
    }));
    fireEvent.click(screen.getByText("refresh-twice"));
    fireEvent.click(screen.getByText("mainnet"));
    fireEvent.click(screen.getByText("start-twice"));

    expect(saveSettings).not.toHaveBeenCalled();
    expect(startEngine).not.toHaveBeenCalled();

    await act(async () => {
      finishAccountRefresh({ data: { totalWalletBalance: "200.00" } });
      finishStatusRefresh({ data: { engine_state: "stopped", running: false } });
    });
  });

  it("does not let refresh responses overwrite newer WebSocket state", async () => {
    let finishAccountRefresh;
    let finishStatusRefresh;
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    getAccountBalance.mockReturnValueOnce(new Promise((resolve) => { finishAccountRefresh = resolve; }));
    getEngineStatus.mockReturnValueOnce(new Promise((resolve) => { finishStatusRefresh = resolve; }));
    fireEvent.click(screen.getByText("refresh-twice"));
    act(() => {
      receiveMessage({ type: "account", data: { totalWalletBalance: "999.00" } });
      receiveMessage({ type: "bot_status", data: { engine_state: "retrying", running: true } });
    });

    await act(async () => {
      finishAccountRefresh({ data: { totalWalletBalance: "200.00" } });
      finishStatusRefresh({ data: { engine_state: "stopped", running: false } });
    });

    expect(screen.getByTestId("account-balance").textContent).toBe("999.00");
    expect(screen.getByTestId("bot-state").textContent).toBe("retrying");
    expect(screen.getByTestId("refresh-revision").textContent).toBe("1");
  });

  it("reports a partial refresh failure without discarding existing data", async () => {
    render(<AppProvider><Probe /></AppProvider>);
    await act(async () => Promise.resolve());

    getAccountBalance.mockRejectedValueOnce(new Error("offline"));
    getEngineStatus.mockResolvedValueOnce({ data: { engine_state: "running", running: true } });
    await act(async () => fireEvent.click(screen.getByText("refresh-twice")));

    expect(screen.getByTestId("account-balance").textContent).toBe("100.00");
    expect(screen.getByTestId("bot-state").textContent).toBe("running");
    expect(screen.getByTestId("refresh-error").textContent).toContain("部分数据刷新失败");
    expect(screen.getByTestId("refresh-revision").textContent).toBe("1");
  });
});
