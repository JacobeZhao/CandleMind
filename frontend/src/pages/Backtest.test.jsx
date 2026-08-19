import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Backtest from "./Backtest";
import { getSarAdxBacktestCapabilities, runSarAdxBacktest } from "../api/client";

vi.mock("../api/client", () => ({
  getSarAdxBacktestCapabilities: vi.fn(),
  runSarAdxBacktest: vi.fn(),
}));

vi.mock("../context/AppContext", () => ({
  useApp: () => ({ symbol: "SOLUSDT" }),
}));

vi.mock("recharts", () => ({
  Area: () => null,
  CartesianGrid: () => null,
  ComposedChart: ({ children }) => <div>{children}</div>,
  Line: () => null,
  ResponsiveContainer: ({ children }) => <div>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

describe("Backtest", () => {
  afterEach(cleanup);

  beforeEach(() => {
    getSarAdxBacktestCapabilities.mockResolvedValue({
      data: { symbols: ["BTCUSDT", "SOLUSDT"], coverage: [] },
    });
    runSarAdxBacktest.mockResolvedValue({ data: {} });
  });

  it("submits normalized strategy parameters for the selected symbol", async () => {
    render(<Backtest />);
    await waitFor(() => expect(getSarAdxBacktestCapabilities).toHaveBeenCalledOnce());
    expect(screen.getByRole("heading", { name: "CandleMind 趋势策略回测" })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/SAR|ADX|V3/i);

    fireEvent.change(screen.getByDisplayValue("10000"), { target: { value: "12500" } });
    fireEvent.click(screen.getByRole("button", { name: "运行回测" }));

    await waitFor(() => expect(runSarAdxBacktest).toHaveBeenCalledWith(expect.objectContaining({
      symbol: "SOLUSDT",
      initial_capital: 12500,
      fee_rate: 0.001,
      slippage_bps: 2,
    })));
    expect((await screen.findByRole("status")).textContent).toContain("回测完成");
  });

  it("rejects an invalid date window without calling the API", async () => {
    render(<Backtest />);
    fireEvent.change(screen.getAllByDisplayValue("2025-01-01")[0], { target: { value: "2026-02-01" } });
    fireEvent.click(screen.getByRole("button", { name: "运行回测" }));

    expect((await screen.findByRole("alert")).textContent).toContain("结束日期必须晚于开始日期");
    expect(runSarAdxBacktest).not.toHaveBeenCalled();
  });

  it("does not expose internal strategy names returned by the API", async () => {
    runSarAdxBacktest.mockResolvedValue({
      data: {
        metrics: { total_return: 0 },
        trades: [{ exit_reason: "SAR reversal", net_pnl_before_funding: 0 }],
        equity_curve: [],
        drawdown_curve: [],
        fills: [],
        funding: [],
        data_lineage: { ohlcv_release_id: "sar_release", funding_release_id: "adx_release" },
        execution: { engine: "sar_adx", engine_version: "V3", bar_count: 0, signal_timing: "ADX close" },
      },
    });
    render(<Backtest />);
    fireEvent.click(screen.getByRole("button", { name: "运行回测" }));

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("回测完成"));
    expect(document.body.textContent).not.toMatch(/SAR|ADX|V3/i);
  });
});
