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
});
