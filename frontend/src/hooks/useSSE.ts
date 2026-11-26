import { useState, useCallback, useRef } from "react";
import type { AuthChoice, SSEEvent } from "../types/events";
import { persistSessionId } from "./useSession";

interface UseSSEReturn {
  isConnected: boolean;
  error: string | null;
  events: SSEEvent[];
  sendQuery: (query: string, sessionId?: string | null) => Promise<string | null>;
  sendAuthDecision: (
    sessionId: string,
    toolCallId: string,
    choice: AuthChoice
  ) => Promise<string | null>;
  clearEvents: () => void;
  abort: () => void;
}

/**
 * 事件去重键。
 *
 * Per-step + 内容指纹，不是整个事件的 JSON。旧的键是
 * `tool_call:${JSON.stringify(event)}` —— agent 连着调三次同一个工具时，
 * 三个事件字符串化后一样，只有第一个能渲染出来，用户看到一张卡片就以为
 * 循环卡死了。
 *
 * 后端给每一轮 LLM 迭代发不同的 `step`（graph_thinking.generate 的
 * state.step_count），所以 step 是天然的碰撞边界。同一个 step + 同样的
 * 内容 = 重发，丢掉。
 */
function sseKey(event: SSEEvent): string {
  const step = event.step ?? -1;
  if (event.type === "thinking") {
    return `thinking:${step}:${event.content ?? ""}`;
  }
  if (event.type === "text") {
    return `text:${step}:${JSON.stringify(event.content)}`;
  }
  if (event.type === "tool_call") {
    return `tool_call:${step}:${event.name ?? ""}:${JSON.stringify(event.input)}`;
  }
  if (event.type === "tool_result") {
    // tool_call_id 参与指纹 —— 同一步里两个不同的工具结果不能互相顶掉
    return `tool_result:${step}:${event.tool_call_id ?? ""}:${event.name ?? ""}:${event.result ?? ""}`;
  }
  if (event.type === "auth_request") {
    return `auth_request:${step}:${event.tool_call_id ?? ""}`;
  }
  if (event.type === "done") {
    // `done` 是终态 —— 只有第一个有用，后面的会带着过期的 session_id 覆盖状态
    return "done";
  }
  return `${event.type}:${step}:${JSON.stringify(event)}`;
}

/**
 * 读一条 SSE 流，把事件交给 onEvent；返回流里带的 session_id。
 *
 * 提问和鉴权裁决两条路都用它 —— 事件格式一样，解析方式就该一样。
 */
async function consumeSse(
  response: Response,
  onEvent: (event: SSEEvent) => void
): Promise<string | null> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  const seenKeys = new Set<string>();
  let returnedSessionId: string | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const chunk = decoder.decode(value, { stream: true });
    const lines = chunk.split("\n");

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
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

        const key = sseKey(event);
        if (seenKeys.has(key)) continue;
        seenKeys.add(key);

        onEvent(event);

        if (event.type === "done") {
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

  return returnedSessionId;
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

        return await consumeSse(response, (event) => {
          setEvents((prev) => [...prev, event]);
          if (event.type === "done") setIsConnected(false);
        });
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

  /**
   * 用户裁决一个待批的工具调用。
   *
   * 后端补齐 tool_result 后会重跑图 —— 所以响应也是一条 SSE 流，事件照常
   * 进 setEvents，前端接着渲染。
   */
  const sendAuthDecision = useCallback(
    async (
      sessionId: string,
      toolCallId: string,
      choice: AuthChoice
    ): Promise<string | null> => {
      setError(null);
      setIsConnected(true);
      setEvents([]);

      try {
        const response = await fetch("http://127.0.0.1:8000/auth/respond", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            tool_call_id: toolCallId,
            choice,
          }),
        });

        if (!response.ok) {
          throw new Error(`HTTP error: ${response.status}`);
        }

        return await consumeSse(response, (event) => {
          setEvents((prev) => [...prev, event]);
          if (event.type === "done") setIsConnected(false);
        });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
        setIsConnected(false);
        return null;
      }
    },
    []
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
    sendAuthDecision,
    clearEvents,
    abort,
  };
}
