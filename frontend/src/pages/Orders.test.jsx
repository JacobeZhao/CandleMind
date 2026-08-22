import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Orders from "./Orders";
import {
  getAccountTradingAnalytics,
  getCombinedOpenOrders,
  getOrderHistory,
  getRecentTrades,
} from "../api/client";
import { refreshMountedReaders } from "../services/refreshCoordinator";

const appState = {
  networkTab: "test",
  symbol: "SOLUSDT",
  exchangeProvider: "binance",
  exchangeSupported: true,
  exchangeSwitching: false,
};

const completeAnalytics = {
  schema_version: "1",
  scope: { network: "testnet", symbol: "SOLUSDT" },
  as_of: "2026-08-20T08:00:00Z",
  coverage: { status: "complete", from: "2026-08-01T00:00:00Z", through: "2026-08-20T08:00:00Z", reasons: [], sync_state: "synced" },
  counts: { status: "complete", completed_total: 12, long: 7, short: 5 },
  week: { net_pnl_usdt: 25.5, net_return_pct: 1.25, status: "complete" },
  month: { net_pnl_usdt: -10, net_return_pct: -0.5, status: "complete" },
  overall: { completed_count: 12, long: 7, short: 5, win_count: 7, loss_count: 5, win_rate_pct: 58.333, payoff_ratio: 1.75, status: "complete", reasons: [] },
};

const combinedOrders = {
  scope: { network: "testnet", symbol: "SOLUSDT" },
  as_of: "2026-08-20T08:00:00Z",
  status: "complete",
  reasons: [],
  orders: [
    { id: "regular-1", source: "regular", time: 1_700_000_000_000, symbol: "SOLUSDT", side: "BUY", type: "LIMIT", origQty: "1", price: "100", stopPrice: "0", status: "NEW" },
    { id: "algo-1", source: "algo", createTime: 1_700_000_001_000, symbol: "SOLUSDT", side: "SELL", orderType: "STOP_MARKET", quantity: "2", price: "0", triggerPrice: "90", status: "NEW" },
  ],
};

vi.mock("../context/AppContext", () => ({ useApp: () => appState }));
vi.mock("../api/client", () => ({
  getAccountTradingAnalytics: vi.fn(),
  getCombinedOpenOrders: vi.fn(),
  getOrderHistory: vi.fn(),
  getRecentTrades: vi.fn(),
}));

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

describe("Orders", () => {
  beforeEach(() => {
    appState.networkTab = "test";
    appState.symbol = "SOLUSDT";
    appState.exchangeProvider = "binance";
    appState.exchangeSupported = true;
    appState.exchangeSwitching = false;
    getAccountTradingAnalytics.mockResolvedValue({ data: completeAnalytics });
    getCombinedOpenOrders.mockResolvedValue({ data: combinedOrders });
    getRecentTrades.mockResolvedValue({ data: [] });
    getOrderHistory.mockResolvedValue({ data: [] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("loads combined regular and Algo orders immediately and renders account statistics", async () => {
    render(<Orders />);

    expect(getCombinedOpenOrders).toHaveBeenCalledWith("SOLUSDT", expect.any(AbortSignal));
    expect(getAccountTradingAnalytics).toHaveBeenCalledWith("SOLUSDT", expect.any(AbortSignal));
    expect(await screen.findByText("Algo")).toBeTruthy();
    expect(screen.getByText("普通")).toBeTruthy();
    expect(screen.getByText("90.00")).toBeTruthy();
    expect(screen.getByText("账户交易统计")).toBeTruthy();
    expect(screen.getByText("+25.50 USDT")).toBeTruthy();
    expect(screen.getByText("58.33%")).toBeTruthy();
    expect(screen.queryByText("资金上限")).toBeNull();
    expect(screen.queryByText("策略分析")).toBeNull();
  });

  it("reloads open orders for symbol, network, and coordinated global refresh", async () => {
    const view = render(<Orders />);
    await screen.findByText("Algo");
    const firstSignal = getCombinedOpenOrders.mock.calls[0][1];

    getCombinedOpenOrders.mockResolvedValueOnce({ data: {
      ...combinedOrders,
      scope: { network: "testnet", symbol: "BTCUSDT" },
      orders: [{ ...combinedOrders.orders[0], id: "btc", symbol: "BTCUSDT", price: "200" }],
    } });
    getAccountTradingAnalytics.mockResolvedValueOnce({ data: { ...completeAnalytics, scope: { network: "testnet", symbol: "BTCUSDT" } } });
    appState.symbol = "BTCUSDT";
    view.rerender(<Orders />);
    expect(await screen.findByText("200.00")).toBeTruthy();
    expect(firstSignal.aborted).toBe(true);

    getCombinedOpenOrders.mockResolvedValueOnce({ data: { ...combinedOrders, scope: { network: "mainnet", symbol: "BTCUSDT" }, orders: [] } });
    getAccountTradingAnalytics.mockResolvedValueOnce({ data: { ...completeAnalytics, scope: { network: "mainnet", symbol: "BTCUSDT" } } });
    appState.networkTab = "main";
    view.rerender(<Orders />);
    expect(await screen.findByText("暂无挂单")).toBeTruthy();

    getCombinedOpenOrders.mockResolvedValueOnce({ data: { ...combinedOrders, scope: { network: "mainnet", symbol: "BTCUSDT" }, orders: [] } });
    getAccountTradingAnalytics.mockResolvedValueOnce({ data: { ...completeAnalytics, scope: { network: "mainnet", symbol: "BTCUSDT" } } });
    await act(async () => refreshMountedReaders());
    await waitFor(() => expect(getCombinedOpenOrders).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(getAccountTradingAnalytics).toHaveBeenCalledTimes(4));
  });

  it("ignores a late open-order response from the previous scope", async () => {
    const oldRequest = deferred();
    getCombinedOpenOrders
      .mockReturnValueOnce(oldRequest.promise)
      .mockResolvedValueOnce({ data: {
        ...combinedOrders,
        scope: { network: "testnet", symbol: "BTCUSDT" },
        orders: [{ ...combinedOrders.orders[0], id: "new", symbol: "BTCUSDT", price: "222" }],
      } });
    const view = render(<Orders />);
    const oldSignal = getCombinedOpenOrders.mock.calls[0][1];

    appState.symbol = "BTCUSDT";
    getAccountTradingAnalytics.mockResolvedValueOnce({ data: { ...completeAnalytics, scope: { network: "testnet", symbol: "BTCUSDT" } } });
    view.rerender(<Orders />);
    expect(await screen.findByText("222.00")).toBeTruthy();
    expect(oldSignal.aborted).toBe(true);

    oldRequest.resolve({ data: combinedOrders });
    await Promise.resolve();
    expect(screen.queryByText("100.00")).toBeNull();
  });

  it("distinguishes loading, partial, empty, error, and stale open-order states", async () => {
    const initial = deferred();
    getCombinedOpenOrders.mockReturnValueOnce(initial.promise);
    const view = render(<Orders />);
    expect(screen.getByText("正在加载挂单")).toBeTruthy();

    initial.resolve({ data: { ...combinedOrders, status: "partial", warnings: ["algo_orders_unavailable"], orders: [combinedOrders.orders[0]] } });
    expect(await screen.findByText(/部分数据：Algo 挂单暂不可用/)).toBeTruthy();
    expect(screen.getByText("普通")).toBeTruthy();

    getCombinedOpenOrders.mockRejectedValueOnce({ response: { data: { detail: "挂单接口暂不可用" } } });
    fireEvent.click(screen.getByRole("button", { name: "刷新订单数据" }));
    expect(await screen.findByText(/数据可能已过期：挂单接口暂不可用/)).toBeTruthy();
    expect(screen.getByText("普通")).toBeTruthy();

    getCombinedOpenOrders.mockResolvedValueOnce({ data: { ...combinedOrders, orders: [] } });
    fireEvent.click(screen.getByRole("button", { name: "刷新订单数据" }));
    expect(await screen.findByText("暂无挂单")).toBeTruthy();

    getCombinedOpenOrders.mockRejectedValueOnce({ response: { data: { detail: "挂单读取失败" } } });
    fireEvent.click(screen.getByRole("button", { name: "刷新订单数据" }));
    expect((await screen.findByRole("alert")).textContent).toContain("挂单读取失败");
  });

  it("keeps zero account metrics valid and labels missing closed-cycle metrics", async () => {
    getAccountTradingAnalytics.mockResolvedValue({ data: {
      ...completeAnalytics,
      coverage: { ...completeAnalytics.coverage, status: "partial", reasons: ["history_retention_limit"] },
      counts: { status: "partial", completed_total: 0, long: 0, short: 0 },
      week: { status: "partial", return_status: "partial", net_pnl_usdt: 0, net_return_pct: 0 },
      month: { status: "partial", return_status: "partial", net_pnl_usdt: 0, net_return_pct: 0 },
      overall: { status: "partial", completed_count: 0, long: 0, short: 0, win_count: 0, loss_count: 0, win_rate_pct: null, payoff_ratio: null },
    } });
    render(<Orders />);

    expect(await screen.findAllByText("+0.00 USDT")).toHaveLength(2);
    expect(screen.getAllByText("+0.00%")).toHaveLength(2);
    expect(screen.getAllByText("0")).toHaveLength(2);
    expect(screen.getAllByText("暂无样本")).toHaveLength(2);
    expect(screen.getByText("数据覆盖不足")).toBeTruthy();
    expect(document.body.textContent).not.toContain("Infinity");
  });

  it("rejects an analytics response for another account scope", async () => {
    getAccountTradingAnalytics.mockResolvedValue({ data: {
      ...completeAnalytics,
      scope: { network: "mainnet", symbol: "SOLUSDT" },
    } });
    render(<Orders />);

    expect((await screen.findByRole("alert")).textContent).toContain("账户交易统计范围不一致");
  });

  it("keeps history and trade tabs working with independent requests", async () => {
    getRecentTrades.mockResolvedValueOnce({ data: [{ id: 2, time: 1_700_000_000_100, symbol: "SOLUSDT", side: "BUY", price: "222", qty: "1", commission: "0.1", commissionAsset: "USDT", realizedPnl: "2" }] });
    getOrderHistory.mockResolvedValueOnce({ data: [{ orderId: 7, time: 1_700_000_000_000, symbol: "SOLUSDT", side: "SELL", type: "MARKET", price: "0", avgPrice: "98.5", origQty: "2", status: "FILLED" }] });
    render(<Orders />);
    await screen.findByText("Algo");

    fireEvent.click(screen.getByRole("tab", { name: "交易所成交记录" }));
    expect(await screen.findByText("222.00")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "历史订单" }));
    expect(await screen.findByText("98.50")).toBeTruthy();
  });

  it("retains same-scope trades on a transient retry failure", async () => {
    getRecentTrades.mockResolvedValueOnce({ data: [{ id: 2, time: 1_700_000_000_100, symbol: "SOLUSDT", side: "BUY", price: "222", qty: "1" }] });
    render(<Orders />);
    fireEvent.click(screen.getByRole("tab", { name: "交易所成交记录" }));
    expect(await screen.findByText("222.00")).toBeTruthy();
    getRecentTrades.mockRejectedValueOnce({ response: { status: 503, data: { detail: "成交记录暂不可用" } } });

    fireEvent.click(screen.getByRole("button", { name: "刷新订单数据" }));

    expect((await screen.findByRole("alert")).textContent).toContain("显示上次成功数据");
    expect(screen.getByText("222.00")).toBeTruthy();
  });

  it("exposes tabs with selection state and supports keyboard navigation", async () => {
    render(<Orders />);
    await screen.findByText("Algo");
    const tabs = screen.getAllByRole("tab");
    expect(screen.getByRole("tablist", { name: "订单数据视图" })).toBeTruthy();
    expect(tabs[0].getAttribute("aria-selected")).toBe("true");
    expect(tabs[1].tabIndex).toBe(-1);

    fireEvent.keyDown(tabs[0], { key: "ArrowRight" });

    expect(screen.getByRole("tab", { name: "交易所成交记录" }).getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "交易所成交记录" }));
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe("orders-tab-1");
  });

  it("shows complete -2015 guidance and does not retain protected orders", async () => {
    getCombinedOpenOrders.mockRejectedValueOnce({ response: { status: 401, data: { code: -2015, msg: "Invalid API-key, IP, or permissions" } } });
    render(<Orders />);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("API Key 无效");
    expect(alert.textContent).toContain("合约交易权限");
    expect(alert.textContent).toContain("后端服务器出口 IP");
    expect(screen.queryByText("普通")).toBeNull();
  });

  it("unmounts Binance readers and clears the view when the exchange becomes unavailable", async () => {
    const pendingOrders = deferred();
    getCombinedOpenOrders.mockReturnValueOnce(pendingOrders.promise);
    const view = render(<Orders />);
    const signal = getCombinedOpenOrders.mock.calls[0][1];

    appState.exchangeProvider = "okx";
    appState.exchangeSupported = false;
    view.rerender(<Orders />);

    expect(signal.aborted).toBe(true);
    expect(screen.getByRole("heading", { name: "OKX 未连接" })).toBeTruthy();
    expect(screen.getByText("未来会接入，敬请期待")).toBeTruthy();
    expect(screen.queryByLabelText("订单明细")).toBeNull();
  });
});
