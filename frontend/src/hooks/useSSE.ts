import { useState, useCallback, useRef } from "react";
import type { SSEEvent } from "../types/events";
import { persistSessionId } from "./useSession";

interface UseSSEReturn {
  isConnected: boolean;
  error: string | null;
  events: SSEEvent[];
  sendQuery: (query: string, sessionId?: string | null) => Promise<string | null>;
  clearEvents: () => void;
  abort: () => void;
}

export function useSSE(
  endpoint: string = "http://127.0.0.1:8000/session/query"
): UseSSEReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<SSEEvent[]>([]);
  const abortControllerRef = useRef<AbortController | null>(null);

  const sendQuery = useCallback(
    async (query: string, sessionId?: string | null): Promise<string | null> => {
      // [LABPILOT-DEBUG] 入口：记录本次 sendQuery 的所有入参和是否有旧流
      console.log("[LABPILOT-DEBUG] sendQuery ENTRY", {
        query: query.slice(0, 100),
        sessionId,
        hasExistingController: !!abortControllerRef.current,
      });

      if (abortControllerRef.current) {
        // [LABPILOT-DEBUG] abort 旧流 — 这会打断原 SSE 流，可能丢失 done 事件
        console.log("[LABPILOT-DEBUG] sendQuery ABORT previous stream");
        abortControllerRef.current.abort();
      }

      abortControllerRef.current = new AbortController();
      setError(null);
      setIsConnected(true);
      setEvents([]);

      const seenKeys = new Set<string>();

      try {
        const body: { query: string; session_id?: string } = { query };
        if (sessionId) {
          body.session_id = sessionId;
        }

        // [LABPILOT-DEBUG] 实际 fetch 前记录 request body
        console.log("[LABPILOT-DEBUG] sendQuery FETCH start", { body });

        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: abortControllerRef.current.signal,
        });

        // [LABPILOT-DEBUG] fetch 响应状态
        console.log("[LABPILOT-DEBUG] sendQuery FETCH response", {
          status: response.status,
          ok: response.ok,
        });

        if (!response.ok) {
          throw new Error(`HTTP error: ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let returnedSessionId: string | null = null;

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

                // [LABPILOT-DEBUG] 每个 SSE 事件都打（type + session_id），用来追踪 done 何时到达
                console.log("[LABPILOT-DEBUG] SSE EVENT", {
                  type: event.type,
                  hasSessionId: !!(event as { session_id?: string }).session_id,
                  sessionId: (event as { session_id?: string }).session_id,
                });

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
                  // Extract session_id from done event or X-Session-Id header
                  if (event.session_id) {
                    returnedSessionId = event.session_id;
                    // [LABPILOT-DEBUG] 关键：done 事件触发的 session_id 持久化
                    console.log("[LABPILOT-DEBUG] SSE DONE persistSessionId", event.session_id);
                    persistSessionId(event.session_id);
                  } else {
                    console.log("[LABPILOT-DEBUG] SSE DONE but NO session_id in event!");
                  }
                }
              } catch (e) {
                console.error("Failed to parse SSE data:", e);
              }
            }
          }
        }

        return returnedSessionId;
      } catch (e) {
        if (e instanceof Error && e.name === "AbortError") {
          // Request was cancelled
        } else {
          setError(e instanceof Error ? e.message : "Unknown error");
          setIsConnected(false);
        }
        return null;
      }
    },
    [endpoint]
  );

  const clearEvents = useCallback(() => {
    setEvents([]);
    setError(null);
  }, []);

  const abort = useCallback(() => {
    abortControllerRef.current?.abort();
  }, []);

  return {
    isConnected,
    error,
    events,
    sendQuery,
    clearEvents,
    abort,
  };
}
