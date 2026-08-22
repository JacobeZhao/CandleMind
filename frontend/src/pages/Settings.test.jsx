import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "./Settings";
import * as client from "../api/client";
import { refreshMountedReaders } from "../services/refreshCoordinator";

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
};

const appState = {
  exchangeProvider: "binance",
  exchangeSwitching: false,
  exchangeError: null,
  switchExchange: vi.fn(),
  setBinanceConnected: vi.fn(),
};

vi.mock("../context/AppContext", () => ({
  useApp: () => appState,
}));

vi.mock("../api/client", () => ({
  getSettings: vi.fn(),
  saveSettings: vi.fn(),
  getMyIp: vi.fn(),
  testConnection: vi.fn(),
  listAIProviders: vi.fn(),
  listAIConfigs: vi.fn(),
  createAIConfig: vi.fn(),
  updateAIConfig: vi.fn(),
  deleteAIConfig: vi.fn(),
  activateAIConfig: vi.fn(),
  testAIConfig: vi.fn(),
  testAIConfigDraft: vi.fn(),
}));

describe("Settings", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.clearAllMocks();
    appState.exchangeProvider = "binance";
    appState.exchangeSwitching = false;
    appState.exchangeError = null;
    client.getSettings.mockResolvedValue({
      data: { test_key_set: false, main_key_set: false, proxy_url: "", connected: false, exchange_provider: "binance" },
    });
    client.saveSettings.mockResolvedValue({ data: { message: "保存成功" } });
    client.getMyIp.mockResolvedValue({ data: { ip: "203.0.113.1", via_proxy: false } });
    client.listAIProviders.mockResolvedValue({ data: [] });
    client.listAIConfigs.mockResolvedValue({ data: [] });
  });

  it("detects the outbound IP on mount and every 60 seconds", async () => {
    vi.useFakeTimers();
    try {
      render(<Settings />);
      await act(async () => Promise.resolve());

      expect(client.getMyIp).toHaveBeenCalledOnce();
      expect(client.getMyIp.mock.calls[0][0]).toBeInstanceOf(AbortSignal);

      await act(async () => vi.advanceTimersByTimeAsync(59_999));
      expect(client.getMyIp).toHaveBeenCalledOnce();

      await act(async () => vi.advanceTimersByTimeAsync(1));
      expect(client.getMyIp).toHaveBeenCalledTimes(2);
    } finally {
      cleanup();
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it("retains the previous result while a scheduled detection is pending", async () => {
    vi.useFakeTimers();
    const nextDetection = deferred();
    client.getMyIp
      .mockResolvedValueOnce({ data: { ip: "203.0.113.10", via_proxy: true } })
      .mockReturnValueOnce(nextDetection.promise);
    try {
      render(<Settings />);
      await act(async () => Promise.resolve());
      expect(screen.getByText("203.0.113.10")).toBeTruthy();

      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(screen.getByText("203.0.113.10")).toBeTruthy();
      expect(screen.getByRole("button", { name: /检测中/ }).disabled).toBe(true);

      await act(async () => nextDetection.resolve({ data: { ip: "203.0.113.11", via_proxy: false } }));
      expect(screen.getByText("203.0.113.11")).toBeTruthy();
    } finally {
      cleanup();
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it("prevents scheduled and manual detections from overlapping", async () => {
    vi.useFakeTimers();
    const pending = deferred();
    client.getMyIp.mockReturnValueOnce(pending.promise);
    try {
      render(<Settings />);
      await act(async () => Promise.resolve());
      const detectButton = screen.getByRole("button", { name: /检测中/ });
      expect(detectButton.disabled).toBe(true);

      fireEvent.click(detectButton);
      await act(async () => vi.advanceTimersByTimeAsync(180_000));
      expect(client.getMyIp).toHaveBeenCalledOnce();

      await act(async () => pending.resolve({ data: { ip: "203.0.113.12", via_proxy: true } }));
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(client.getMyIp).toHaveBeenCalledTimes(2);
    } finally {
      cleanup();
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it("aborts the active detection and clears polling on unmount", async () => {
    vi.useFakeTimers();
    const pending = deferred();
    client.getMyIp.mockReturnValueOnce(pending.promise);
    try {
      const view = render(<Settings />);
      await act(async () => Promise.resolve());
      const signal = client.getMyIp.mock.calls[0][0];

      view.unmount();
      expect(signal.aborted).toBe(true);

      await act(async () => vi.advanceTimersByTimeAsync(120_000));
      expect(client.getMyIp).toHaveBeenCalledOnce();
    } finally {
      pending.reject(Object.assign(new Error("cancelled"), { code: "ERR_CANCELED" }));
      await Promise.resolve();
      cleanup();
      vi.clearAllTimers();
      vi.useRealTimers();
    }
  });

  it("normalizes structured IP detection errors", async () => {
    client.getMyIp.mockRejectedValueOnce({
      response: { data: { detail: { code: "ip_lookup_failed", message: "出口 IP 服务暂不可用" } } },
    });

    render(<Settings />);

    expect((await screen.findByRole("alert")).textContent).toContain("出口 IP 服务暂不可用");
  });

  it("renders five accessible exchange tabs and switches immediately", async () => {
    render(<Settings />);
    await waitFor(() => expect(client.getSettings).toHaveBeenCalledOnce());

    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Binance", "OKX", "Bybit", "Gate.io", "A股"]);
    expect(screen.getByRole("tab", { name: "Binance" }).getAttribute("aria-selected")).toBe("true");

    fireEvent.click(screen.getByRole("tab", { name: "OKX" }));
    expect(appState.switchExchange).toHaveBeenCalledWith("okx");
  });

  it("shows only the unavailable message, disables save, and keeps IP detection available", async () => {
    appState.exchangeProvider = "gateio";
    client.getMyIp.mockResolvedValue({ data: { ip: "203.0.113.8", via_proxy: true } });
    render(<Settings />);
    await waitFor(() => expect(client.getSettings).toHaveBeenCalledOnce());

    expect(screen.getByText("未来会接入，敬请期待")).toBeTruthy();
    expect(screen.queryByText("Binance API 配置")).toBeNull();
    expect(screen.getByRole("button", { name: /保存配置/ }).disabled).toBe(true);
    expect(client.listAIProviders).not.toHaveBeenCalled();
    expect(client.listAIConfigs).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /检测出口 IP/ }));
    expect(await screen.findByText("203.0.113.8")).toBeTruthy();
  });

  it("moves focus between exchange tabs with arrow keys without switching", async () => {
    render(<Settings />);
    await waitFor(() => expect(client.getSettings).toHaveBeenCalledOnce());
    const binance = screen.getByRole("tab", { name: "Binance" });
    const okx = screen.getByRole("tab", { name: "OKX" });
    binance.focus();

    fireEvent.keyDown(binance, { key: "ArrowRight" });

    expect(document.activeElement).toBe(okx);
    expect(appState.switchExchange).not.toHaveBeenCalled();
  });

  it("refreshes read-only settings without clearing unsaved secret input", async () => {
    render(<Settings />);
    await waitFor(() => expect(client.getSettings).toHaveBeenCalledOnce());
    const keyInput = screen.getAllByPlaceholderText("输入 API Key")[0];
    fireEvent.change(keyInput, { target: { value: "unsaved-key" } });

    await act(async () => refreshMountedReaders());

    await waitFor(() => expect(client.getSettings).toHaveBeenCalledTimes(2));
    expect(keyInput.value).toBe("unsaved-key");
  });

  it("keeps the mounted refresh pending until all settings readers settle", async () => {
    let finishConfigs;
    render(<Settings />);
    await waitFor(() => expect(client.getSettings).toHaveBeenCalledOnce());
    client.listAIConfigs.mockReturnValueOnce(new Promise((resolve) => { finishConfigs = resolve; }));

    let refresh;
    await act(async () => {
      refresh = refreshMountedReaders();
      await Promise.resolve();
    });
    let settled = false;
    refresh.then(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);

    await act(async () => finishConfigs({ data: [] }));
    expect((await refresh).find((result) => result.key === "settings:page")).toMatchObject({
      status: "fulfilled",
      value: true,
    });
  });

  it("sends secrets once and clears them after a successful save", async () => {
    render(<Settings />);
    await waitFor(() => expect(client.getSettings).toHaveBeenCalledOnce());
    const keyInputs = screen.getAllByPlaceholderText("输入 API Key");
    const secretInputs = screen.getAllByPlaceholderText("输入 API Secret");

    fireEvent.change(keyInputs[0], { target: { value: "test-key" } });
    fireEvent.change(secretInputs[0], { target: { value: "test-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /保存配置/ }));

    await waitFor(() => expect(client.saveSettings).toHaveBeenCalledWith(expect.objectContaining({
      api_key_test: "test-key",
      api_secret_test: "test-secret",
    })));
    await waitFor(() => expect(keyInputs[0].value).toBe(""));
    expect(secretInputs[0].value).toBe("");
    expect(screen.getByText("保存成功")).toBeTruthy();
  });

  const openNewAiConfig = async () => {
    fireEvent.click(screen.getByRole("button", { name: /管理模型/ }));
    const addButton = await screen.findByRole("button", { name: /新增模型/ });
    fireEvent.click(addButton);
  };

  it("uses stable DeepSeek defaults when provider metadata fails to load", async () => {
    client.listAIProviders.mockRejectedValue(new Error("provider metadata unavailable"));

    render(<Settings />);
    await openNewAiConfig();

    expect(screen.getByPlaceholderText("留空使用默认").value).toBe("https://api.deepseek.com");
    expect(screen.getByPlaceholderText("例：deepseek-v4-pro").value).toBe("deepseek-v4-flash");
  });

  it("prefers loaded provider metadata over local DeepSeek defaults", async () => {
    client.listAIProviders.mockResolvedValue({
      data: [{
        id: "deepseek",
        name: "DeepSeek",
        base_url: "https://provider.example/deepseek",
        model: "provider-model",
      }],
    });

    render(<Settings />);
    await waitFor(() => expect(client.listAIProviders).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: /管理模型/ })).toBeTruthy());
    await openNewAiConfig();

    expect(screen.getByPlaceholderText("留空使用默认").value).toBe("https://provider.example/deepseek");
    expect(screen.getByPlaceholderText("例：deepseek-v4-pro").value).toBe("provider-model");
  });

  it("shows structured draft errors without persisting or exposing the API key", async () => {
    const apiKey = "sk-secret-that-must-not-render";
    const message = "Base URL 不能指向未授权的私有或本地地址";
    client.testAIConfigDraft.mockRejectedValue({
      response: { data: { detail: { code: "invalid_base_url", message, retryable: false } } },
    });

    render(<Settings />);
    await openNewAiConfig();

    fireEvent.change(screen.getByPlaceholderText("例：我的DeepSeek"), {
      target: { value: "DeepSeek 主配置" },
    });
    fireEvent.change(screen.getAllByPlaceholderText("输入 API Key").at(-1), {
      target: { value: apiKey },
    });
    fireEvent.click(screen.getByRole("button", { name: /保存并测试/ }));

    expect(await screen.findByText(message)).toBeTruthy();
    expect(client.testAIConfigDraft).toHaveBeenCalledWith(expect.objectContaining({
      name: "DeepSeek 主配置",
      provider: "deepseek",
      api_key: apiKey,
      base_url: "https://api.deepseek.com",
      model_name: "deepseek-v4-flash",
    }));
    expect(client.createAIConfig).not.toHaveBeenCalled();
    expect(client.updateAIConfig).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(apiKey);
  });
});
