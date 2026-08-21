const listeners = new Set();

export function marketAgentEventFromMessage(message) {
  if (message?.type !== "market_agent_event") return null;
  const payload = message.data?.event ?? message.event ?? message.data;
  if (!payload || typeof payload !== "object") return null;
  return payload;
}

export function isCompleteMarketAgentEvent(message) {
  const event = marketAgentEventFromMessage(message);
  return Boolean(event && (
    Object.prototype.hasOwnProperty.call(event, "content")
    || Object.prototype.hasOwnProperty.call(event, "answer")
  ));
}

export function publishRealtimeEvent(message) {
  listeners.forEach((listener) => listener(message));
}

export function subscribeRealtimeEvent(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
