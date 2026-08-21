import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Header from "./Header";
import { getSymbols } from "../api/client";
import { MemoryRouter } from "react-router-dom";

const appState = {
  botStatus: null,
  botStatusLoaded: true,
  connected: true,
  symbol: "SOLUSDT",
  networkTab: "test",
  networkSwitching: false,
  networkError: null,
  strategyCommandPending: false,
  strategyCommandError: null,
  strategyStatusUncertain: false,
  symbolSwitching: false,
  strategyCapitalLimit: "1000",
  strategyConfigurationLoaded: true,
  strategyConfigurationError: null,
  strategyConfiguration: {
    strategy_type: "sar_adx_trend",
    config_version: "sar_adx_trend_v1",
    config_hash: "trend-config-hash",
    parameters: { execution_interval: "5m", sar_step: 0.02, sar_max: 0.2, adx_threshold: 45, max_layers: 5 },
  },
  refreshRevision: 0,
  refreshPending: false,
  refreshError: null,
  refreshAll: vi.fn(),
  startStrategy: vi.fn(),
  stopStrategy: vi.fn(),
  setSymbol: vi.fn(),
  switchNetwork: vi.fn(),
};

vi.mock("../context/AppContext", () => ({
  useApp: () => appState,
}));

vi.mock("../api/client", () => ({
  getSymbols: vi.fn(),
}));

describe("Header network controls", () => {
  const renderHeader = (path = "/orders") => render(<MemoryRouter initialEntries={[path]}><Header /></MemoryRouter>);

  beforeEach(() => {
    appState.networkTab = "test";
    appState.networkSwitching = false;
    appState.networkError = null;
    appState.strategyCommandPending = false;
    appState.strategyCommandError = null;
    appState.strategyStatusUncertain = false;
    appState.symbolSwitching = false;
    appState.refreshRevision = 0;
    appState.refreshPending = false;
    appState.refreshError = null;
    getSymbols.mockResolvedValue({ data: ["SOLUSDT"] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps both network choices available in the responsive header", async () => {
    renderHeader();
    await waitFor(() => expect(getSymbols).toHaveBeenCalledOnce());

    const testnet = screen.getByRole("button", { name: "测试网" });
    const mainnet = screen.getByRole("button", { name: "真实网" });
    expect(testnet.closest(".hidden")).toBeNull();
    expect(mainnet.closest(".hidden")).toBeNull();
    fireEvent.click(mainnet);
    expect(appState.switchNetwork).toHaveBeenCalledWith("main");
  });

  it("disables network choices while switching and shows the backend error", () => {
    appState.networkSwitching = true;
    appState.networkError = "目标网络不可用";
    renderHeader();

    expect(screen.getByRole("button", { name: "测试网" }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: /真实网/ }).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("目标网络不可用");
  });

  it.each(["/markets", "/orders", "/strategies"])("shows strategy control on %s", (path) => {
    renderHeader(path);
    expect(screen.getByRole("button", { name: "启动策略" })).toBeTruthy();
  });

  it("places the strategy control before the network choices", () => {
    renderHeader("/markets");
    const strategy = screen.getByRole("button", { name: "启动策略" });
    const testnet = screen.getByRole("button", { name: "测试网" });
    expect(strategy.compareDocumentPosition(testnet) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("places a refresh button beside the larger symbol selector", async () => {
    renderHeader();
    const symbol = screen.getByRole("button", { name: /SOLUSDT/ });
    const refresh = screen.getByRole("button", { name: "刷新当前数据" });
    expect(symbol.className).toContain("text-base");
    expect(symbol.className).toContain("font-semibold");
    expect(symbol.parentElement.parentElement).toBe(refresh.parentElement);

    fireEvent.click(refresh);
    expect(appState.refreshAll).toHaveBeenCalledOnce();
  });

  it("requires the exact symbol-scoped confirmation on mainnet", async () => {
    appState.networkTab = "main";
    appState.startStrategy.mockResolvedValue(true);
    renderHeader();

    fireEvent.click(screen.getByRole("button", { name: "启动策略" }));
    const confirm = screen.getByRole("button", { name: "确认真实网启动" });
    fireEvent.change(screen.getByLabelText("真实网确认文本"), { target: { value: "MAINNET:BTCUSDT" } });
    expect(confirm.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("真实网确认文本"), { target: { value: "MAINNET:SOLUSDT" } });
    fireEvent.click(confirm);

    await waitFor(() => expect(appState.startStrategy).toHaveBeenCalledWith("MAINNET:SOLUSDT"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("disables network and strategy commands while a strategy command is pending", () => {
    appState.strategyCommandPending = true;
    renderHeader();
    expect(screen.getByRole("button", { name: "测试网" }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "启动策略" }).disabled).toBe(true);
  });

  it("disables refresh while refreshing", () => {
    appState.refreshPending = true;
    renderHeader();
    const refresh = screen.getByRole("button", { name: "刷新当前数据" });
    expect(refresh.disabled).toBe(true);
    expect(refresh.querySelector("svg").classList.contains("animate-spin")).toBe(true);
  });

  it("confirms symbol, network and capital before starting on testnet", async () => {
    appState.startStrategy.mockResolvedValue(true);
    renderHeader();

    fireEvent.click(screen.getByRole("button", { name: "启动策略" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("SOLUSDT");
    expect(dialog.textContent).toContain("测试网");
    expect(dialog.textContent).toContain("1000 USDT");
    expect(dialog.textContent).toContain("CandleMind趋势策略");
    expect(dialog.textContent).toContain("SAR 加速因子 0.02");
    fireEvent.click(screen.getByRole("button", { name: "确认启动" }));

    await waitFor(() => expect(appState.startStrategy).toHaveBeenCalledWith(undefined));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps the confirmation open when the start command is rejected", async () => {
    appState.startStrategy.mockResolvedValue(false);
    renderHeader();

    fireEvent.click(screen.getByRole("button", { name: "启动策略" }));
    fireEvent.click(screen.getByRole("button", { name: "确认启动" }));

    await waitFor(() => expect(appState.startStrategy).toHaveBeenCalledWith(undefined));
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});
