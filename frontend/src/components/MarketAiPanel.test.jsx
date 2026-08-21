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
import { publishRealtimeEvent } from "../services/realtimeEvents";

const appState = vi.hoisted(() => ({ connected: true }));

vi.mock("../context/AppContext", () => ({
  useApp: () => appState,
}));

vi.mock("../api/client", () => ({
  getMarketAgentEvents: vi.fn(),
  getMarketAgentStatus: vi.fn(),
  sendMarketAgentMessage: vi.fn(),
  startMarketAgent: vi.fn(),
  stopMarketAgent: vi.fn(),
}));

function status(overrides = {}) {
  return {
    state: "running",
    enabled: true,
    desired_enabled: true,
    agent_id: "agent-1",
    symbol: "SOLUSDT",
    latest_sequence: 0,
    ...overrides,
  };
}

function agentEvent(sequence, overrides = {}) {
  return {
    sequence,
    type: "analysis",
    role: "assistant",
    content: `第 ${sequence} 条完整分析\n更多证据`,
    short_summary: `第 ${sequence} 条摘要`,
    agent_id: "agent-1",
    symbol: "SOLUSDT",
    batch_id: `batch-${sequence}`,
    created_at: `2026-08-21T00:0${sequence}:00Z`,
    reasons: ["candle_closed"],
    ...overrides,
  };
}

describe("MarketAiPanel", () => {
  afterEach(cleanup);

  beforeEach(() => {
    appState.connected = true;
    sendMarketAgentMessage.mockResolvedValue({ status: 200, data: { type: "assistant_message" } });
    getMarketAgentStatus.mockResolvedValue({ data: status({ state: "stopped", enabled: false, desired_enabled: false, agent_id: null, symbol: null }) });
    getMarketAgentEvents.mockResolvedValue({ data: { events: [], latest_sequence: 0 } });
    startMarketAgent.mockResolvedValue({ data: status() });
    stopMarketAgent.mockResolvedValue({ data: status({ state: "stopped", enabled: false, desired_enabled: false }) });
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("bootstraps status and events and renders a compact analysis with folded evidence", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status({ latest_sequence: 1 }) });
    getMarketAgentEvents.mockResolvedValue({ data: { events: [agentEvent(1)], latest_sequence: 1 } });

    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);

    expect(await screen.findByText("第 1 条摘要")).toBeTruthy();
    expect(screen.queryByText("更多证据")).toBeNull();
    expect(screen.getByText("证据与风险")).toBeTruthy();
    expect(getMarketAgentStatus.mock.calls[0][0]).toBeInstanceOf(AbortSignal);
    expect(getMarketAgentEvents).toHaveBeenCalledWith(0, 100, expect.any(AbortSignal));
  });

  it("merges a complete sequential WebSocket event without another REST event request", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status({ latest_sequence: 1 }) });
    getMarketAgentEvents.mockResolvedValue({ data: { events: [agentEvent(1)], latest_sequence: 1 } });
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await screen.findByText("第 1 条摘要");
    const callsBefore = getMarketAgentEvents.mock.calls.length;

    publishRealtimeEvent({ type: "market_agent_event", data: agentEvent(2) });

    expect(await screen.findByText("第 2 条摘要")).toBeTruthy();
    expect(getMarketAgentEvents).toHaveBeenCalledTimes(callsBefore);
  });

  it("uses REST to fill a sequence gap announced by metadata-only WebSocket events", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status({ latest_sequence: 1 }) });
    getMarketAgentEvents.mockImplementation((afterSequence) => Promise.resolve({
      data: afterSequence === 0
        ? { events: [agentEvent(1)], latest_sequence: 1 }
        : { events: [agentEvent(2), agentEvent(3)], latest_sequence: 3 },
    }));
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await screen.findByText("第 1 条摘要");
    getMarketAgentStatus.mockResolvedValue({ data: status({ latest_sequence: 3 }) });

    publishRealtimeEvent({
      type: "market_agent_event",
      data: { agent_id: "agent-1", symbol: "SOLUSDT", sequence: 3, event_type: "analysis" },
    });

    expect(await screen.findByText("第 3 条摘要")).toBeTruthy();
    expect(getMarketAgentEvents).toHaveBeenCalledWith(1, 100, expect.any(AbortSignal));
  });

  it("groups correlated user and assistant events while retaining uncorrelated legacy replies", async () => {
    const events = [
      agentEvent(1, { type: "user_message", role: "user", content: "趋势如何？", short_summary: undefined, client_message_id: "message-1" }),
      agentEvent(2, { type: "assistant_message", content: "趋势向上", short_summary: "趋势向上", client_message_id: "message-1" }),
      agentEvent(3, { type: "assistant_message", content: "旧版独立回复", short_summary: undefined, client_message_id: undefined }),
    ];
    getMarketAgentStatus.mockResolvedValue({ data: status({ latest_sequence: 3 }) });
    getMarketAgentEvents.mockResolvedValue({ data: { events, latest_sequence: 3 } });

    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);

    expect(await screen.findByText("趋势如何？")).toBeTruthy();
    expect(screen.getByText("趋势向上")).toBeTruthy();
    expect(screen.getByText("旧版独立回复")).toBeTruthy();
    expect(screen.getAllByText("助手回复")).toHaveLength(2);
  });

  it("sends a quick question with a client id and supports a 202 queued response", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status() });
    sendMarketAgentMessage.mockResolvedValue({ status: 202, data: { state: "queued", job_id: "job-1" } });
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "现在的市场周期是什么？" }).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "现在的市场周期是什么？" }));

    await waitFor(() => expect(sendMarketAgentMessage).toHaveBeenCalledWith({
      symbol: "SOLUSDT",
      content: "现在的市场周期是什么？",
    }, expect.any(AbortSignal), expect.any(String)));
    expect(await screen.findByText("排队中")).toBeTruthy();
  });

  it("does not steal scroll while reading history and exposes a new-analysis control", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status({ latest_sequence: 1 }) });
    getMarketAgentEvents.mockResolvedValue({ data: { events: [agentEvent(1)], latest_sequence: 1 } });
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await screen.findByText("第 1 条摘要");
    await waitFor(() => expect(Element.prototype.scrollIntoView).toHaveBeenCalled());
    Element.prototype.scrollIntoView.mockClear();
    const feed = screen.getByTestId("market-agent-feed");
    Object.defineProperties(feed, {
      clientHeight: { configurable: true, value: 100 },
      scrollHeight: { configurable: true, value: 1000 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    });
    fireEvent.scroll(feed);

    publishRealtimeEvent({ type: "market_agent_event", data: agentEvent(2) });

    const newEvents = await screen.findByRole("button", { name: "1 条新解读" });
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    fireEvent.click(newEvents);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("recovers through REST when the shared WebSocket reconnects", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status() });
    const view = render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await waitFor(() => expect(getMarketAgentEvents).toHaveBeenCalled());
    const callsBefore = getMarketAgentEvents.mock.calls.length;

    appState.connected = false;
    view.rerender(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    appState.connected = true;
    view.rerender(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);

    await waitFor(() => expect(getMarketAgentEvents.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it("does not commit a stale REST response after the symbol scope changes", async () => {
    let resolveOldEvents;
    getMarketAgentStatus
      .mockResolvedValueOnce({ data: status({ latest_sequence: 1 }) })
      .mockResolvedValue({
        data: status({ agent_id: "agent-2", symbol: "BTCUSDT", latest_sequence: 1 }),
      });
    getMarketAgentEvents
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOldEvents = resolve; }))
      .mockResolvedValue({
        data: {
          events: [agentEvent(1, {
            agent_id: "agent-2",
            symbol: "BTCUSDT",
            content: "BTC 完整分析",
            short_summary: "BTC 新作用域",
          })],
          latest_sequence: 1,
        },
      });
    const view = render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await waitFor(() => expect(getMarketAgentEvents).toHaveBeenCalledTimes(1));

    view.rerender(<MarketAiPanel onClose={vi.fn()} symbol="BTCUSDT" />);
    expect(await screen.findByText("BTC 新作用域")).toBeTruthy();
    resolveOldEvents({ data: { events: [agentEvent(1, { short_summary: "SOL 旧响应" })], latest_sequence: 1 } });

    await waitFor(() => expect(screen.queryByText("SOL 旧响应")).toBeNull());
    expect(screen.getByText("BTC 新作用域")).toBeTruthy();
  });

  it("shows exact paused-budget lifecycle data", async () => {
    getMarketAgentStatus.mockResolvedValue({
      data: status({ state: "paused_budget", daily_usage_count: 20, daily_usage_limit: 20 }),
    });
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    expect(await screen.findByText("预算暂停")).toBeTruthy();
    expect(screen.getByText("今日预算 20/20")).toBeTruthy();
  });

  it("starts the persistent agent for the displayed market", async () => {
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);
    await screen.findByRole("button", { name: "启动" });
    fireEvent.click(screen.getByRole("button", { name: "启动" }));
    await waitFor(() => expect(startMarketAgent).toHaveBeenCalledWith({ symbol: "SOLUSDT" }));
    expect(stopMarketAgent).not.toHaveBeenCalled();
  });

  it("collapses without stopping the persistent agent", async () => {
    const onClose = vi.fn();
    render(<MarketAiPanel onClose={onClose} symbol="SOLUSDT" />);
    fireEvent.click(screen.getByRole("button", { name: "收起实时助手" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(stopMarketAgent).not.toHaveBeenCalled();
  });

  it("uses the compact real-time assistant header and composer", () => {
    render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" />);

    expect(screen.getByRole("region", { name: "实时行情助手" })).toBeTruthy();
    expect(screen.queryByText("VibeTrading 实时助手")).toBeNull();
    expect(screen.queryByText(/SOLUSDT · 多周期 · 仅分析已收盘 K 线/)).toBeNull();
    expect(screen.queryByText("AI 只读分析，不会执行交易；内容不构成投资建议。")).toBeNull();

    const composer = screen.getByPlaceholderText("助手运行后可提问");
    expect(composer.getAttribute("rows")).toBe("1");
    expect(composer.className).toContain("h-10");
    expect(composer.className).toContain("overflow-y-auto");
    expect(screen.getByRole("button", { name: "发送问题" }).className).toContain("h-10");
  });

  it("does not stop a running agent when only the visual interval changes", async () => {
    getMarketAgentStatus.mockResolvedValue({ data: status() });
    const view = render(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    await waitFor(() => expect(getMarketAgentStatus).toHaveBeenCalled());
    view.rerender(<MarketAiPanel onClose={vi.fn()} symbol="SOLUSDT" interval="1h" />);
    expect(screen.getByRole("region", { name: "实时行情助手" })).toBeTruthy();
    expect(stopMarketAgent).not.toHaveBeenCalled();
  });
});
