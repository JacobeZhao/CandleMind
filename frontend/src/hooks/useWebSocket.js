import { useEffect, useRef } from "react";

export function useWebSocket(onMessage, onConnectionChange) {
  const ws = useRef(null);
  const onMessageRef = useRef(onMessage);
  const onConnectionChangeRef = useRef(onConnectionChange);
  onMessageRef.current = onMessage;
  onConnectionChangeRef.current = onConnectionChange;

  useEffect(() => {
    let active = true;
    let reconnectTimer = null;

    const connect = () => {
      if (!active) return;
      const protocol = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${protocol}://${window.location.host}/ws`;
      const socket = new WebSocket(url);
      ws.current = socket;

      socket.onopen = () => console.log("WS connected");
      socket.onmessage = (event) => {
        if (!active) return;
        try {
          onMessageRef.current(JSON.parse(event.data));
        } catch {}
      };
      socket.onclose = () => {
        if (!active) return;
        onConnectionChangeRef.current?.(false);
        console.log("WS closed, reconnecting in 3s...");
        reconnectTimer = setTimeout(connect, 3000);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      active = false;
      if (ws.current) ws.current.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      onConnectionChangeRef.current?.(false);
    };
  }, []);
}
