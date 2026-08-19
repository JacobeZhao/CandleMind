import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import WorkspaceDivider from "./WorkspaceDivider";

function dispatchPointer(target, type, properties) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.entries(properties).forEach(([key, value]) => {
    Object.defineProperty(event, key, { value });
  });
  fireEvent(target, event);
}

describe("WorkspaceDivider", () => {
  beforeEach(() => {
    vi.stubGlobal("requestAnimationFrame", (callback) => {
      callback();
      return 1;
    });
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("supports keyboard resizing with bounded values", () => {
    const onChange = vi.fn();
    const onCommit = vi.fn();
    render(
      <WorkspaceDivider
        orientation="vertical"
        value={420}
        min={320}
        max={550}
        onChange={onChange}
        onCommit={onCommit}
      />,
    );
    const divider = screen.getByRole("separator");

    fireEvent.keyDown(divider, { key: "ArrowLeft" });
    expect(onChange).toHaveBeenLastCalledWith(436);
    expect(onCommit).toHaveBeenLastCalledWith(436);

    fireEvent.keyDown(divider, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith(550);
  });

  it("throttles pointer changes and commits after the drag", () => {
    const onChange = vi.fn();
    const onCommit = vi.fn();
    render(
      <WorkspaceDivider
        orientation="vertical"
        value={420}
        min={320}
        max={550}
        onChange={onChange}
        onCommit={onCommit}
      />,
    );
    const divider = screen.getByRole("separator");

    dispatchPointer(divider, "pointerdown", { pointerId: 4, button: 0, clientX: 600 });
    dispatchPointer(divider, "pointermove", { pointerId: 4, clientX: 560 });
    dispatchPointer(divider, "pointerup", { pointerId: 4, clientX: 560 });

    expect(onChange).toHaveBeenCalledWith(460);
    expect(onCommit).toHaveBeenCalledWith(460);
  });
});
