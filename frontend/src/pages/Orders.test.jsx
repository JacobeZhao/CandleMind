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
      data: {
        running: false,
        engine_state: "stopped",
        circuit_open: false,
        paper_fill_count: 0,
        paper_fill_count_complete: true,
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

  it("distinguishes paper fills from exchange trade records", async () => {
    render(<Orders />);

    await waitFor(() => expect(screen.getByText("策略纸面成交：0 笔")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "交易所成交记录" }));
    expect(await screen.findByText("暂无交易所成交记录")).toBeTruthy();
  });

  it("shows an unknown paper fill count as unavailable", async () => {
    getEngineStatus.mockResolvedValue({
      data: {
        running: false,
        engine_state: "stopped",
        paper_fill_count: null,
        paper_fill_count_complete: false,
      },
    });

    render(<Orders />);
    await waitFor(() => expect(screen.getByText("策略纸面成交：--")).toBeTruthy());
  });

  it("shows recovery-required as a distinct engine state", async () => {
    getEngineStatus.mockResolvedValue({
      data: {
        running: false,
        engine_state: "recovery_required",
        paper_fill_count: 2,
        paper_fill_count_complete: true,
      },
    });

    render(<Orders />);

    await waitFor(() => expect(screen.getByText("需要恢复")).toBeTruthy());
    expect(screen.queryByText("未运行")).toBeNull();
  });
});
