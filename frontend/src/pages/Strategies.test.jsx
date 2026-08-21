import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Strategies from "./Strategies";
import { getStrategyCatalog, saveStrategyConfig } from "../api/client";

const appState = {
  botStatus: { running: false },
  strategyConfiguration: {
    strategy_type: "sar_adx_trend",
    config_version: "sar_adx_trend_v1",
    config_hash: "saved-trend-hash",
    parameters: { execution_interval: "5m", sar_step: 0.02, sar_max: 0.2, max_layers: 5, adx_timeframe: "1h", adx_period: 14, adx_threshold: 45, entry_confirmation_bars: 6 },
  },
  strategyConfigurationError: null,
  loadStrategyConfiguration: vi.fn(),
  setStrategyConfiguration: vi.fn(),
};

vi.mock("../context/AppContext", () => ({ useApp: () => appState }));
vi.mock("../api/client", () => ({
  getStrategyCatalog: vi.fn(),
  saveStrategyConfig: vi.fn(),
}));

describe("Strategies", () => {
  beforeEach(() => {
    appState.botStatus = { running: false };
    appState.strategyConfigurationError = null;
    appState.loadStrategyConfiguration.mockResolvedValue(appState.strategyConfiguration);
    getStrategyCatalog.mockResolvedValue({ data: { strategies: [] } });
    saveStrategyConfig.mockResolvedValue({
      data: {
        strategy_type: "sar_martingale",
        config_version: "sar_martingale_v1",
        config_hash: "martingale-hash",
        parameters: { execution_interval: "5m", sar_step: 0.02, sar_max: 0.2, max_layers: 4, layer_multiplier: 1.5, add_trigger_fraction: 0.005 },
      },
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("loads three keyboard-operable strategy choices and the saved parameters", async () => {
    render(<Strategies />);
    await screen.findByRole("button", { name: /CandleMind趋势策略/ });

    const cards = screen.getAllByRole("button", { pressed: false });
    expect(cards).toHaveLength(2);
    expect(screen.getByRole("button", { name: /CandleMind趋势策略/ }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByLabelText("SAR 加速因子").value).toBe("0.02");
    expect(screen.queryByText("运行回测")).toBeNull();
  });

  it("switches dynamic fields and saves the selected strategy", async () => {
    render(<Strategies />);
    const martingale = await screen.findByRole("button", { name: /SAR马丁/ });
    fireEvent.click(martingale);

    expect(screen.getByLabelText("逆向加仓幅度").value).toBe("0.5");
    fireEvent.change(screen.getByLabelText("仓位倍率"), { target: { value: "1.6" } });
    fireEvent.click(screen.getByRole("button", { name: "保存策略配置" }));

    await waitFor(() => expect(saveStrategyConfig).toHaveBeenCalledWith({
      strategy_type: "sar_martingale",
      parameters: expect.objectContaining({ layer_multiplier: 1.6, add_trigger_fraction: 0.005 }),
      expected_config_hash: "saved-trend-hash",
    }));
    expect(appState.setStrategyConfiguration).toHaveBeenCalledWith(expect.objectContaining({
      strategy_type: "sar_martingale",
      config_hash: "martingale-hash",
    }));
    expect((await screen.findByRole("status")).textContent).toContain("全局启动");
  });

  it("blocks invalid cross-field values before sending a request", async () => {
    render(<Strategies />);
    await screen.findByRole("button", { name: /CandleMind趋势策略/ });
    fireEvent.change(screen.getByLabelText("SAR 加速因子"), { target: { value: "0.1" } });
    fireEvent.change(screen.getByLabelText("SAR 最大加速因子"), { target: { value: "0.05" } });

    expect(screen.getByText("必须大于或等于 SAR 加速因子")).toBeTruthy();
    expect(screen.getByRole("button", { name: "保存策略配置" }).disabled).toBe(true);
    expect(saveStrategyConfig).not.toHaveBeenCalled();
  });

  it("locks selection and editing while a strategy is running", async () => {
    appState.botStatus = { running: true };
    render(<Strategies />);
    await screen.findByRole("button", { name: /CandleMind趋势策略/ });

    expect(screen.getByRole("button", { name: /SAR反马丁/ }).disabled).toBe(true);
    expect(screen.getByLabelText("SAR 加速因子").disabled).toBe(true);
    expect(screen.getByRole("button", { name: "保存策略配置" }).disabled).toBe(true);
  });
});
