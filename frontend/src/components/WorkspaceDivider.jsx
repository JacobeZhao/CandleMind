import React, { useEffect, useRef } from "react";

const KEYBOARD_STEP = 16;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

export default function WorkspaceDivider({
  orientation = "vertical",
  value,
  min,
  max,
  onChange,
  onCommit,
}) {
  const dragRef = useRef(null);
  const frameRef = useRef(null);
  const pendingRef = useRef(value);

  useEffect(() => {
    pendingRef.current = value;
  }, [value]);

  useEffect(() => () => {
    if (frameRef.current != null) cancelAnimationFrame(frameRef.current);
  }, []);

  const scheduleChange = (nextValue) => {
    pendingRef.current = clamp(nextValue, min, max);
    if (frameRef.current != null) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null;
      onChange(pendingRef.current);
    });
  };

  const pointerCoordinate = (event) => (
    orientation === "vertical" ? event.clientX : event.clientY
  );

  const onPointerDown = (event) => {
    if (event.button != null && event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      coordinate: pointerCoordinate(event),
      value,
    };
  };

  const onPointerMove = (event) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const delta = pointerCoordinate(event) - drag.coordinate;
    scheduleChange(drag.value - delta);
  };

  const finishPointer = (event) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    dragRef.current = null;
    if (frameRef.current != null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
      onChange(pendingRef.current);
    }
    onCommit(pendingRef.current);
  };

  const onKeyDown = (event) => {
    let nextValue = value;
    if (event.key === "Home") nextValue = min;
    else if (event.key === "End") nextValue = max;
    else if (orientation === "vertical" && event.key === "ArrowLeft") nextValue += KEYBOARD_STEP;
    else if (orientation === "vertical" && event.key === "ArrowRight") nextValue -= KEYBOARD_STEP;
    else if (orientation === "horizontal" && event.key === "ArrowUp") nextValue += KEYBOARD_STEP;
    else if (orientation === "horizontal" && event.key === "ArrowDown") nextValue -= KEYBOARD_STEP;
    else return;

    event.preventDefault();
    const clamped = clamp(nextValue, min, max);
    onChange(clamped);
    onCommit(clamped);
  };

  return (
    <div
      role="separator"
      tabIndex={0}
      aria-label="调整行情图与 AI 助手的大小"
      aria-orientation={orientation}
      aria-valuemin={Math.round(min)}
      aria-valuemax={Math.round(max)}
      aria-valuenow={Math.round(value)}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={finishPointer}
      onPointerCancel={finishPointer}
      onKeyDown={onKeyDown}
      className={`group relative shrink-0 touch-none outline-none ${orientation === "vertical" ? "w-2 cursor-col-resize" : "h-2 cursor-row-resize"}`}
    >
      <span
        className={`absolute bg-border transition-colors group-hover:bg-accent group-focus-visible:bg-accent ${orientation === "vertical" ? "inset-y-0 left-1/2 w-px -translate-x-1/2" : "inset-x-0 top-1/2 h-px -translate-y-1/2"}`}
      />
    </div>
  );
}
