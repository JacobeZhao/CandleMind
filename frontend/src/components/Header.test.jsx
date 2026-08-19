import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Header from "./Header";
import { getSymbols } from "../api/client";

const appState = {
  botStatus: null,
  botStatusLoaded: true,
  connected: true,
  symbol: "SOLUSDT",
  networkTab: "test",
  networkSwitching: false,
  networkError: null,
  setSymbol: vi.fn(),
  switchNetwork: vi.fn(),
};

vi.mock("../context/AppContext", () => ({
  useApp: () => appState,
}));

vi.mock("../api/client", () => ({
  getSymbols: vi.fn(),
}));

describe("Header network controls", () => {
  beforeEach(() => {
    appState.networkTab = "test";
    appState.networkSwitching = false;
    appState.networkError = null;
    getSymbols.mockResolvedValue({ data: ["SOLUSDT"] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("keeps both network choices available in the responsive header", async () => {
    render(<Header />);
    await waitFor(() => expect(getSymbols).toHaveBeenCalledOnce());

    const testnet = screen.getByRole("button", { name: "测试网" });
    const mainnet = screen.getByRole("button", { name: "真实网" });
    expect(testnet.closest(".hidden")).toBeNull();
    expect(mainnet.closest(".hidden")).toBeNull();
    fireEvent.click(mainnet);
    expect(appState.switchNetwork).toHaveBeenCalledWith("main");
  });

  it("disables network choices while switching and shows the backend error", () => {
    appState.networkSwitching = true;
    appState.networkError = "目标网络不可用";
    render(<Header />);

    expect(screen.getByRole("button", { name: "测试网" }).disabled).toBe(true);
    expect(screen.getByRole("button", { name: /真实网/ }).disabled).toBe(true);
    expect(screen.getByRole("alert").textContent).toContain("目标网络不可用");
  });
});
