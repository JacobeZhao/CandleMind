import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Orders from "./Orders";
import { getEngineStatus, startEngine } from "../api/client";

const appState = {
  networkTab: "test",
  symbol: "SOLUSDT",
  openOrders: [
    {
      orderId: 42,
      time: 1_700_000_000_000,
      symbol: "SOLUSDT",
      side: "BUY",
      type: "LIMIT",
      origQty: "1",
      price: "100",
      stopPrice: "0",
      status: "NEW",
    },
  ],
};

vi.mock("../context/AppContext", () => ({
  useApp: () => appState,
}));

vi.mock("../api/client", () => ({
  getOrderHistory: vi.fn(),
  getRecentTrades: vi.fn(),
  startEngine: vi.fn(),
  stopEngine: vi.fn(),
  getEngineStatus: vi.fn(),
}));

describe("Orders", () => {
  beforeEach(() => {
    appState.networkTab = "test";
    getEngineStatus.mockResolvedValue({
      data: {
        running: false,
        engine_state: "stopped",
        circuit_open: false,
        network: "testnet",
        decision_count: 3,
        submitted_order_count: 2,
        filled_order_count: 1,
        rejected_order_count: 0,
        unknown_order_count: 0,
      },
    });
    startEngine.mockResolvedValue({ data: { message: "已启动" } });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders open orders as a read-only table", async () => {
    render(<Orders />);

    expect(screen.getByText("SOLUSDT")).toBeTruthy();
    expect(screen.queryByText("操作")).toBeNull();
    expect(screen.queryByText("撤单")).toBeNull();
    await waitFor(() => expect(getEngineStatus).toHaveBeenCalled());
  });

  it("starts the selected strategy with exchange execution parameters", async () => {
    render(<Orders />);

    fireEvent.click(screen.getByRole("button", { name: "启动策略" }));

    await waitFor(() => {
      expect(startEngine).toHaveBeenCalledWith({
        strategy_type: "sar_adx_pyramid",
        config_version: "sar_adx_v3",
        symbol: "SOLUSDT",
        capital_limit: 1000,
      });
    });
    expect(screen.getByText("CandleMind 趋势策略")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/SAR|ADX|V3/i);
  });

  it("shows exchange execution counters and records", async () => {
    render(<Orders />);

    await waitFor(() => expect(screen.getByText((_, element) => (
      element.tagName === "SPAN" && element.textContent === "已成交 1"
    ))).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "交易所成交记录" }));
    expect(await screen.findByText("暂无交易所成交记录")).toBeTruthy();
  });

  it("requires an exact confirmation before starting on mainnet", async () => {
    appState.networkTab = "main";
    render(<Orders />);
    fireEvent.click(screen.getByRole("button", { name: "启动策略" }));

    expect(screen.getByRole("dialog", { name: "确认启动真实网交易" })).toBeTruthy();
    const confirmButton = screen.getByRole("button", { name: "确认真实网启动" });
    expect(confirmButton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("真实网确认文本"), { target: { value: "MAINNET:SOLUSDT" } });
    fireEvent.click(confirmButton);

    await waitFor(() => expect(startEngine).toHaveBeenCalledWith(expect.objectContaining({
      symbol: "SOLUSDT",
      capital_limit: 1000,
      mainnet_confirmation: "MAINNET:SOLUSDT",
    })));
    appState.networkTab = "test";
  });

  it("shows recovery-required as a distinct engine state", async () => {
    getEngineStatus.mockResolvedValue({
      data: {
        running: false,
        engine_state: "recovery_required",
        filled_order_count: 2,
      },
    });

    render(<Orders />);

    await waitFor(() => expect(screen.getByText("需要恢复")).toBeTruthy());
    expect(screen.queryByText("未运行")).toBeNull();
  });
});
