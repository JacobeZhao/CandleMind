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
    getEngineStatus.mockResolvedValue({
      data: { running: false, circuit_open: false },
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

  it("starts the selected SAR+ADX strategy in paper mode", async () => {
    render(<Orders />);

    fireEvent.click(screen.getByRole("button", { name: "启动策略" }));

    await waitFor(() => {
      expect(startEngine).toHaveBeenCalledWith({
        strategy_type: "sar_adx_pyramid",
        config_version: "sar_adx_v3",
        symbol: "SOLUSDT",
        paper: true,
        initial_capital: 10000,
      });
    });
  });
});
