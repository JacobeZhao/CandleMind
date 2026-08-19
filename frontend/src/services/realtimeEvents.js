const listeners = new Set();

export function publishRealtimeEvent(message) {
  listeners.forEach((listener) => listener(message));
}

export function subscribeRealtimeEvent(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
