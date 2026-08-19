import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MarketAiPanel from "./MarketAiPanel";
import {
  getMarketAgentEvents,
  getMarketAgentStatus,
  sendMarketAgentMessage,
  startMarketAgent,
  stopMarketAgent,
} from "../api/client";

vi.mock("../api/client", () => ({
  getMarketAgentEvents: vi.fn(),
  getMarketAgentStatus: vi.fn(),
  sendMarketAgentMessage: vi.fn(),
  startMarketAgent: vi.fn(),
  stopMarketAgent: vi.fn(),
}));

describe("MarketAiPanel", () => {
  afterEach(cleanup);

  beforeEach(() => {
    sendMarketAgentMessage.mockResolvedValue({ data: { type: "assistant_message" } });
    getMarketAgentStatus.mockResolvedValue({ data: { state: "stopped", enabled: false } });
    getMarketAgentEvents.mockResolvedValue({ data: { events: [] } });
    startMarketAgent.mockResolvedValue({ data: { state: "running", enabled: true, agent_id: "agent-1", symbol: "SOLUSDT", interval: "5m" } });
    stopMarketAgent.mockResolvedValue({ data: { state: "stopped", enabled: false } });
  });

  it("sends a quick question with the current market context", async () => {
    getMarketAgentStatus.mockResolvedValue({
      data: { state: "running", enabled: true, agent_id: "agent-1", symbol: "SOLUSDT" },
    });
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "现在的市场周期是什么？" }).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "现在的市场周期是什么？" }));

    await waitFor(() => expect(sendMarketAgentMessage).toHaveBeenCalledWith({
      symbol: "SOLUSDT",
      content: "现在的市场周期是什么？",
    }, expect.any(AbortSignal)));
  });

  it("uses an in-page region instead of modal semantics", () => {
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    expect(screen.getByRole("region", { name: "VibeTrading 实时助手" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("starts the persistent agent for the displayed market", async () => {
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    fireEvent.click(screen.getByRole("button", { name: "启动" }));
    await waitFor(() => expect(startMarketAgent).toHaveBeenCalledWith({ symbol: "SOLUSDT" }));
    expect(stopMarketAgent).not.toHaveBeenCalled();
  });

  it("collapses without stopping the persistent agent", async () => {
    const onClose = vi.fn();
    render(<MarketAiPanel onClose={onClose} symbol="SOLUSDT" interval="5m" />);
    fireEvent.click(screen.getByRole("button", { name: "收起实时助手" }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(stopMarketAgent).not.toHaveBeenCalled();
  });

  it("does not stop a running agent when only the visual interval changes", async () => {
    getMarketAgentStatus.mockResolvedValue({
      data: { state: "running", enabled: true, agent_id: "agent-1", symbol: "SOLUSDT", interval: "5m" },
    });
    const view = render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    await waitFor(() => expect(getMarketAgentStatus).toHaveBeenCalled());

    view.rerender(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="1h" />);
    await waitFor(() => expect(screen.getByText(/SOLUSDT · 多周期/)).toBeTruthy());
    expect(stopMarketAgent).not.toHaveBeenCalled();
  });
});
