import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Orders from "./Orders";
import { getOrderHistory, getRecentTrades, getStrategyAnalytics } from "../api/client";

const appState = {
  networkTab: "test",
  refreshRevision: 0,
  symbol: "SOLUSDT",
  strategyCapitalLimit: "1000",
  setStrategyCapitalLimit: vi.fn(),
  openOrders: [{ orderId: 42, time: 1_700_000_000_000, symbol: "SOLUSDT", side: "BUY", type: "LIMIT", origQty: "1", price: "100", stopPrice: "0", status: "NEW" }],
};

const completeAnalytics = {
  schema_version: "1",
  scope: { network: "test", symbol: "SOLUSDT", strategy_type: "internal" },
  as_of: "2026-08-20T08:00:00Z",
  coverage: { status: "complete", from: "2026-08-01T00:00:00Z", through: "2026-08-20T08:00:00Z", reasons: [], sync_state: "synced" },
  counts: { status: "complete", completed_total: 12, long: 7, short: 5 },
  week: { net_pnl_usdt: 25.5, net_return_pct: 1.25, status: "complete", reason: null },
  month: { net_pnl_usdt: -10, net_return_pct: -0.5, status: "complete", reason: null },
  overall: { completed_count: 12, long: 7, short: 5, win_count: 7, loss_count: 5, win_rate_pct: 58.333, payoff_ratio: 1.75, status: "complete", reasons: [] },
  costs: { commission_usdt: 2, funding_net_usdt: 1, total_cost_usdt: 3, complete: true },
  equity_curve: [{ time: "2026-08-01T00:00:00Z", equity_usdt: 1000, equity_index: 1 }, { time: "2026-08-20T08:00:00Z", equity_usdt: 1015.5, equity_index: 1.0155 }],
};

vi.mock("../context/AppContext", () => ({ useApp: () => appState }));
vi.mock("../api/client", () => ({ getOrderHistory: vi.fn(), getRecentTrades: vi.fn(), getStrategyAnalytics: vi.fn() }));

describe("Orders", () => {
  beforeEach(() => {
    appState.networkTab = "test";
    appState.refreshRevision = 0;
    appState.symbol = "SOLUSDT";
    appState.strategyCapitalLimit = "1000";
    getStrategyAnalytics.mockResolvedValue({ data: completeAnalytics });
    getRecentTrades.mockResolvedValue({ data: [] });
    getOrderHistory.mockResolvedValue({ data: [] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders analytics formatting and the read-only open-orders tab", async () => {
    render(<Orders />);

    expect(screen.getByText("SOLUSDT")).toBeTruthy();
    expect(screen.queryByText("操作")).toBeNull();
    expect(screen.queryByText("撤单")).toBeNull();
    expect(await screen.findByText("+1.25%")).toBeTruthy();
    expect(screen.getByText("-0.50%")).toBeTruthy();
    expect(screen.getByText("+25.50 USDT")).toBeTruthy();
    expect(screen.getByText("-10.00 USDT")).toBeTruthy();
    expect(screen.getByText("多头交易")).toBeTruthy();
    expect(screen.getByText("空头交易")).toBeTruthy();
    expect(screen.getByText("58.33%")).toBeTruthy();
    expect(screen.getByText("1.75")).toBeTruthy();
    expect(screen.queryByText("资金曲线")).toBeNull();
    expect(screen.queryByText("完成交易")).toBeNull();
    expect(screen.getAllByText("SOLUSDT · 测试网").length).toBe(2);
    expect(document.body.textContent).not.toMatch(/SAR|ADX|V3/i);
    expect(getStrategyAnalytics).toHaveBeenCalledWith(expect.any(AbortSignal));
  });

  it("shows partial sample values while keeping unavailable returns explicit", async () => {
    getStrategyAnalytics.mockResolvedValue({ data: {
      ...completeAnalytics,
      coverage: { ...completeAnalytics.coverage, status: "partial" },
      counts: { status: "partial", completed_total: 3, long: 2, short: 1 },
      week: { status: "partial", return_status: "unavailable", net_return_pct: null, net_pnl_usdt: 12.5 },
      month: { status: "partial", return_status: "unavailable", net_return_pct: null, net_pnl_usdt: -4 },
      overall: { status: "partial", completed_count: 3, long: 2, short: 1, win_count: 2, loss_count: 1, win_rate_pct: 66.667, payoff_ratio: 2.5 },
      equity_curve: [],
    } });
    render(<Orders />);

    expect(await screen.findByText("+12.50 USDT")).toBeTruthy();
    expect(screen.getByText("-4.00 USDT")).toBeTruthy();
    expect(screen.getByText("66.67%")).toBeTruthy();
    expect(screen.getByText("2.50")).toBeTruthy();
    expect(screen.getAllByText("部分数据").length).toBeGreaterThanOrEqual(7);
    expect(screen.getAllByText("暂不可用")).toHaveLength(2);
    expect(document.body.textContent).not.toContain("Infinity");
    expect(screen.queryByText("+0.00%")).toBeNull();
  });

  it("refreshes analytics and the active lower tab when the global revision changes", async () => {
    const view = render(<Orders />);
    await screen.findByText("+1.25%");
    fireEvent.click(screen.getByRole("button", { name: "交易所成交记录" }));
    await screen.findByText("暂无交易所成交记录");
    expect(getStrategyAnalytics).toHaveBeenCalledTimes(1);
    expect(getRecentTrades).toHaveBeenCalledTimes(1);

    appState.refreshRevision = 1;
    view.rerender(<Orders />);

    await waitFor(() => expect(getStrategyAnalytics).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getRecentTrades).toHaveBeenCalledTimes(2));
  });

  it("shows analytics loading and backend error states", async () => {
    let rejectAnalytics;
    getStrategyAnalytics.mockReturnValue(new Promise((_, reject) => { rejectAnalytics = reject; }));
    render(<Orders />);
    expect(screen.getByRole("status").textContent).toContain("正在加载策略分析");

    rejectAnalytics({ response: { data: { detail: "分析数据暂不可用" } } });
    expect((await screen.findByRole("alert")).textContent).toContain("分析数据暂不可用");
  });

  it("ignores a stale analytics response after the symbol changes", async () => {
    let resolveOld;
    getStrategyAnalytics
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ data: { ...completeAnalytics, scope: { ...completeAnalytics.scope, symbol: "BTCUSDT" }, counts: { status: "complete", completed_total: 99, long: 60, short: 39 } } });
    const view = render(<Orders />);
    appState.symbol = "BTCUSDT";
    view.rerender(<Orders />);

    expect(await screen.findByText("60")).toBeTruthy();
    resolveOld({ data: completeAnalytics });
    await Promise.resolve();
    expect(screen.queryByText("7")).toBeNull();
  });

  it("provides loading, empty, error, and populated states for lower tabs", async () => {
    let resolveTrades;
    getRecentTrades.mockReturnValueOnce(new Promise((resolve) => { resolveTrades = resolve; }));
    render(<Orders />);
    await screen.findByText("+1.25%");

    fireEvent.click(screen.getByRole("button", { name: "交易所成交记录" }));
    expect(screen.getByRole("status").textContent).toContain("正在加载");
    resolveTrades({ data: [] });
    expect(await screen.findByText("暂无交易所成交记录")).toBeTruthy();

    getOrderHistory.mockRejectedValueOnce({ response: { data: { detail: "历史订单读取失败" } } });
    fireEvent.click(screen.getByRole("button", { name: "历史订单" }));
    expect((await screen.findByRole("alert")).textContent).toContain("历史订单读取失败");

    getOrderHistory.mockResolvedValueOnce({ data: [{ orderId: 7, time: 1_700_000_000_000, symbol: "SOLUSDT", side: "SELL", type: "MARKET", price: "0", avgPrice: "98.5", origQty: "2", status: "FILLED" }] });
    fireEvent.click(screen.getByRole("button", { name: "刷新订单数据" }));
    expect(await screen.findByText("98.50")).toBeTruthy();
  });

  it("aborts and ignores stale lower-tab responses on network change", async () => {
    let resolveOld;
    getRecentTrades
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ data: [{ id: 2, time: 1_700_000_000_100, symbol: "SOLUSDT", side: "BUY", price: "222", qty: "1", commission: "0.1", commissionAsset: "USDT", realizedPnl: "2" }] });
    const view = render(<Orders />);
    fireEvent.click(screen.getByRole("button", { name: "交易所成交记录" }));
    const oldSignal = getRecentTrades.mock.calls[0][1];

    appState.networkTab = "main";
    view.rerender(<Orders />);
    expect(await screen.findByText("222.00")).toBeTruthy();
    expect(oldSignal.aborted).toBe(true);

    resolveOld({ data: [{ id: 1, time: 1_700_000_000_000, symbol: "SOLUSDT", side: "BUY", price: "111", qty: "1", commission: "0.1", commissionAsset: "USDT", realizedPnl: "1" }] });
    await Promise.resolve();
    expect(screen.queryByText("111.00")).toBeNull();
  });
});
