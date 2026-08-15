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

  it("submits normalized SAR+ADX parameters for the selected symbol", async () => {
    render(<Backtest />);
    await waitFor(() => expect(getSarAdxBacktestCapabilities).toHaveBeenCalledOnce());

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
});
