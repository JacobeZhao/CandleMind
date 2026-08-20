import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import Dashboard from "./Dashboard";

let appState;
let tickerState;

vi.mock("../context/AppContext", () => ({
  useApp: () => appState,
}));

vi.mock("../context/MarketTickerContext", () => ({
  useTicker: () => tickerState,
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

function renderDashboard(botStatus, botStatusLoaded = true, ticker = null) {
  tickerState = ticker;
  appState = {
    account: {},
    accountError: null,
    positions: [],
    connected: true,
    botStatus,
    botStatusLoaded,
  };
  return render(<Dashboard />);
}

describe("Dashboard strategy status", () => {
  afterEach(cleanup);

  it("does not invent a direction or fill count before status loads", () => {
    renderDashboard(null, false);

    expect(screen.getByText("加载中")).toBeTruthy();
    expect(screen.getAllByText("--")).toHaveLength(2);
  });

  it("displays ticker data from the external ticker store", () => {
    renderDashboard(null, false, {
      symbol: "SOLUSDT",
      price: "150",
      change: "2.5",
      high: "155",
      low: "145",
      volume: "1000000",
    });

    expect(screen.getByText(/SOLUSDT/)).toBeTruthy();
    expect(screen.getByText("$150.00")).toBeTruthy();
  });

  it("does not display a stale balance after account authentication fails", () => {
    appState = {
      account: null,
      accountError: "Binance 账户读取失败，请检查 API Key、合约权限和出口 IP 白名单。",
      positions: [],
      connected: true,
      botStatus: null,
      botStatusLoaded: false,
    };

    render(<Dashboard />);

    expect(screen.getByRole("alert").textContent).toContain("Binance 账户读取失败");
    expect(screen.getAllByText("--").length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText("$0.00")).toBeNull();
  });

  it("shows the current exchange position and filled order count", () => {
    renderDashboard({
      engine_state: "running",
      running: true,
      position_direction: "NONE",
      filled_order_count: 3,
      last_action: "opened LONG",
    });

    expect(screen.getByText("当前持仓方向")).toBeTruthy();
    expect(screen.getByText("空仓")).toBeTruthy();
    expect(screen.getByText("交易所成交")).toBeTruthy();
    expect(screen.getByText("3 笔")).toBeTruthy();
    expect(screen.getByText("opened LONG")).toBeTruthy();
  });

  it("keeps network failures out of the last strategy action", () => {
    renderDashboard({
      engine_state: "retrying",
      running: true,
      position_direction: "LONG",
      filled_order_count: null,
      last_action: "[SAR+ADX paper] halted: recovery required",
      error: "RemoteDisconnected secret proxy detail",
    });

    expect(screen.getByText("重试中")).toBeTruthy();
    expect(screen.getByText("多头")).toBeTruthy();
    expect(screen.getByText("行情连接中断，正在自动重试。")).toBeTruthy();
    expect(screen.queryByText(/RemoteDisconnected/)).toBeNull();
    expect(screen.queryByText(/recovery required/)).toBeNull();
  });

  it("keeps strategy failure status visible when market data is disconnected", () => {
    appState = {
      account: {},
      accountError: null,
      positions: [],
      connected: false,
      botStatusLoaded: true,
      botStatus: {
        engine_state: "network_halted",
        running: false,
        position_direction: "NONE",
        filled_order_count: 0,
      },
    };

    render(<Dashboard />);

    expect(screen.getByText("行情连接已断开，策略状态仍可查看。")).toBeTruthy();
    expect(screen.getByText("网络故障")).toBeTruthy();
    expect(screen.getByText("空仓")).toBeTruthy();
  });

  it.each([
    ["network_halted", "网络故障", "网络故障，策略已停止。"],
    ["halted", "已安全停止", "策略已安全停止，请检查配置或运行日志。"],
    ["recovery_required", "需要恢复", "策略状态需要人工恢复。"],
  ])("renders %s without exposing the raw error", (engineState, stateLabel, message) => {
    renderDashboard({
      engine_state: engineState,
      running: false,
      position_direction: "SHORT",
      filled_order_count: 1,
      error: "sensitive raw exception",
    });

    expect(screen.getByText(stateLabel)).toBeTruthy();
    expect(screen.getByText(message)).toBeTruthy();
    expect(screen.queryByText(/sensitive raw exception/)).toBeNull();
  });
});
