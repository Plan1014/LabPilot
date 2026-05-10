import { useState, useCallback, useEffect, useRef } from "react";
import type { Notification } from "../types/events";

interface UseWebSocketReturn {
  notifications: Notification[];
  isConnected: boolean;
  error: string | null;
}

export function useWebSocket(
  url: string = "ws://127.0.0.1:8000/ws"
): UseWebSocketReturn {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
        setError(null);
      };

      ws.onmessage = (event) => {
        try {
          const notification = JSON.parse(event.data) as Notification;
          setNotifications((prev) => [...prev, notification]);
        } catch {
          console.error("Failed to parse WebSocket message");
        }
      };

      ws.onclose = () => {
        setIsConnected(false);
        reconnectTimeoutRef.current = setTimeout(connect, 5000);
      };

      ws.onerror = () => {
        setError("WebSocket connection error");
        setIsConnected(false);
      };
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to connect");
      setIsConnected(false);
    }
  }, [url]);

  useEffect(() => {
    connect();

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, [connect]);

  return {
    notifications,
    isConnected,
    error,
  };
}
