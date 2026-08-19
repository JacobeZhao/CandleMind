import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "./Settings";
import * as client from "../api/client";

vi.mock("../context/AppContext", () => ({
  useApp: () => ({ setConnected: vi.fn() }),
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
    client.getSettings.mockResolvedValue({
      data: { test_key_set: false, main_key_set: false, proxy_url: "", connected: false },
    });
    client.saveSettings.mockResolvedValue({ data: { message: "保存成功" } });
    client.listAIProviders.mockResolvedValue({ data: [] });
    client.listAIConfigs.mockResolvedValue({ data: [] });
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
