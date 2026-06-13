import { useEffect, useRef, useCallback } from "react";

export function useWebSocket(onMessage) {
  const ws = useRef(null);
  const reconnectTimer = useRef(null);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const connect = useCallback(() => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${protocol}://${window.location.host}/ws`;
    const socket = new WebSocket(url);

    socket.onopen = () => {
      console.log("WS connected");
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
    };

    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        onMessageRef.current(msg);
      } catch {}
    };

    socket.onclose = () => {
      console.log("WS closed, reconnecting in 3s...");
      reconnectTimer.current = setTimeout(connect, 3000);
    };

    socket.onerror = () => socket.close();
    ws.current = socket;
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (ws.current) ws.current.close();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, [connect]);
}
