import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MarketAiDialog from "./MarketAiDialog";
import { marketChat } from "../api/client";

vi.mock("../api/client", () => ({ marketChat: vi.fn() }));

describe("MarketAiDialog", () => {
  afterEach(cleanup);

  beforeEach(() => {
    marketChat.mockResolvedValue({ data: { answer: "当前处于趋势扩张阶段。" } });
  });

  it("sends a quick question with the current market context", async () => {
    render(<MarketAiDialog open onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    fireEvent.click(screen.getByRole("button", { name: "现在的市场周期是什么？" }));

    await waitFor(() => expect(marketChat).toHaveBeenCalledWith({
      symbol: "SOLUSDT",
      interval: "5m",
      messages: [{ role: "user", content: "现在的市场周期是什么？" }],
    }, expect.any(AbortSignal)));
    expect(await screen.findByText("当前处于趋势扩张阶段。")).toBeTruthy();
  });

  it("does not render or request data while closed", () => {
    render(<MarketAiDialog open={false} onClose={vi.fn()} symbol="SOLUSDT" interval="5m" />);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(marketChat).not.toHaveBeenCalled();
  });
});
