import { useSyncExternalStore } from "react";

let ticker = null;
const listeners = new Set();

function emitChange() {
  listeners.forEach((listener) => listener());
}

export function subscribeTicker(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getTickerSnapshot() {
  return ticker;
}

export function publishTicker(data) {
  if (!data?.symbol) return;
  ticker = ticker?.symbol === data.symbol ? { ...ticker, ...data } : data;
  emitChange();
}

export function clearTicker() {
  if (ticker === null) return;
  ticker = null;
  emitChange();
}

export function useTicker() {
  return useSyncExternalStore(subscribeTicker, getTickerSnapshot, getTickerSnapshot);
}
