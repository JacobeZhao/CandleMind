import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Markets from "./Markets";

vi.mock("../context/AppContext", () => ({
  useApp: () => ({ symbol: "SOLUSDT" }),
}));

vi.mock("../components/MarketSummary", () => ({
  default: ({ symbol }) => <div>{symbol} summary</div>,
}));

vi.mock("../components/PriceChart", () => ({
  default: ({ interval, onIntervalChange, onOpenAssistant, assistantOpen }) => (
    <div data-testid="price-chart" data-interval={interval} data-assistant-open={assistantOpen}>
      <button type="button" onClick={onOpenAssistant}>toggle assistant</button>
      <button type="button" onClick={() => onIntervalChange("1h")}>set interval</button>
    </div>
  ),
}));

vi.mock("../components/MarketAiPanel", () => ({
  default: ({ symbol, onClose }) => (
    <aside aria-label="assistant panel" data-symbol={symbol}>
      <button type="button" onClick={onClose}>collapse panel</button>
    </aside>
  ),
}));

describe("Markets workspace", () => {
  beforeEach(() => {
    window.localStorage.clear();
    Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1200 });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(1100);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(760);
    global.ResizeObserver = class {
      constructor(callback) {
        this.callback = callback;
      }
      observe() {
        this.callback();
      }
      disconnect() {}
    };
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("owns chart interval and opens an in-page assistant at the desktop default width", () => {
    render(<Markets />);
    fireEvent.click(screen.getByRole("button", { name: "toggle assistant" }));

    const panel = screen.getByRole("complementary", { name: "assistant panel" });
    expect(panel.dataset.symbol).toBe("SOLUSDT");
    expect(panel.parentElement.style.width).toBe("420px");
    expect(screen.getByRole("separator").getAttribute("aria-orientation")).toBe("vertical");

    fireEvent.click(screen.getByRole("button", { name: "set interval" }));
    expect(screen.getByTestId("price-chart").dataset.interval).toBe("1h");
  });

  it("collapses only the panel and switches to a vertical split below 900px", () => {
    render(<Markets />);
    fireEvent.click(screen.getByRole("button", { name: "toggle assistant" }));

    act(() => {
      window.innerWidth = 899;
      window.dispatchEvent(new Event("resize"));
    });
    expect(screen.getByRole("separator").getAttribute("aria-orientation")).toBe("horizontal");

    fireEvent.click(screen.getByRole("button", { name: "collapse panel" }));
    expect(screen.queryByRole("complementary", { name: "assistant panel" })).toBeNull();
    expect(screen.getByTestId("price-chart")).toBeTruthy();
  });
});
