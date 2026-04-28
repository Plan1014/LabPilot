import { useState, useCallback, useRef } from "react";
import type { SSEEvent } from "../types/events";

interface UseSSEReturn {
  isConnected: boolean;
  error: string | null;
  events: SSEEvent[];
  sendQuery: (query: string) => Promise<void>;
  clearEvents: () => void;
}

export function useSSE(
  endpoint: string = "http://127.0.0.1:8000/query"
): UseSSEReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<SSEEvent[]>([]);
  const abortControllerRef = useRef<AbortController | null>(null);

  const sendQuery = useCallback(
    async (query: string) => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      abortControllerRef.current = new AbortController();
      setError(null);
      setIsConnected(true);
      setEvents([]);

      // Deduplication set on frontend as safety net
      const seenKeys = new Set<string>();

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query }),
          signal: abortControllerRef.current.signal,
        });

        if (!response.ok) {
          throw new Error(`HTTP error: ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          const chunk = decoder.decode(value, { stream: true });
          const lines = chunk.split("\n");

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              const data = line.slice(6).trim();
              if (!data) continue;

              try {
                const event = JSON.parse(data) as SSEEvent;

                // Frontend deduplication: skip if we've already seen this exact event
                let key: string;
                if (event.type === "thinking") {
                  key = `thinking:${event.content}`;
                } else if (event.type === "text") {
                  key = `text:${JSON.stringify(event.content)}`;
                } else {
                  key = `${event.type}:${JSON.stringify(event)}`;
                }

                if (seenKeys.has(key)) {
                  continue;
                }
                seenKeys.add(key);

                setEvents((prev) => [...prev, event]);

                if (event.type === "done") {
                  setIsConnected(false);
                }
              } catch (e) {
                console.error("Failed to parse SSE data:", e);
              }
            }
          }
        }
      } catch (e) {
        if (e instanceof Error && e.name === "AbortError") {
          // Request was cancelled, not an error
        } else {
          setError(e instanceof Error ? e.message : "Unknown error");
          setIsConnected(false);
        }
      }
    },
    [endpoint]
  );

  const clearEvents = useCallback(() => {
    setEvents([]);
    setError(null);
  }, []);

  return {
    isConnected,
    error,
    events,
    sendQuery,
    clearEvents,
  };
}
