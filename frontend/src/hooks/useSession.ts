import { useState, useCallback } from "react";
import type { SessionMeta, Message, SSEEvent } from "../types/events";

const API_BASE = "http://127.0.0.1:8000";
const SESSION_ID_KEY = "labpilot_session_id";

interface UseSessionReturn {
  sessionId: string | null;
  messages: Message[];
  isLoading: boolean;
  error: string | null;
  createSession: () => void;
  loadSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  listSessions: () => Promise<SessionMeta[]>;
  clearMessages: () => void;
  _setSessionId: (id: string) => void;
}

export function useSession(): UseSessionReturn {
  const [sessionId, setSessionId] = useState<string | null>(
    () => localStorage.getItem(SESSION_ID_KEY)
  );
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createSession = useCallback(() => {
    setSessionId(null);
    localStorage.removeItem(SESSION_ID_KEY);
    setMessages([]);
  }, []);

  const loadSession = useCallback(async (id: string) => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/session/${id}/history`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setSessionId(id);
      localStorage.setItem(SESSION_ID_KEY, id);

      setMessages(parseHistoryMessages(data.messages));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load session");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const deleteSession = useCallback(async (id: string) => {
    try {
      const res = await fetch(`${API_BASE}/session/${id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (id === sessionId) {
        createSession();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete session");
    }
  }, [sessionId, createSession]);

  const listSessions = useCallback(async (): Promise<SessionMeta[]> => {
    const res = await fetch(`${API_BASE}/session/list`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data.sessions;
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
  }, []);

  const setSessionIdAndPersist = useCallback((id: string) => {
    setSessionId(id);
    localStorage.setItem(SESSION_ID_KEY, id);
  }, []);

  return {
    sessionId,
    messages,
    isLoading,
    error,
    createSession,
    loadSession,
    deleteSession,
    listSessions,
    clearMessages,
    // Expose for internal use
    _setSessionId: setSessionIdAndPersist,
  };
}

// Export internal setter for use by useSSE hook
export function persistSessionId(sessionId: string) {
  localStorage.setItem(SESSION_ID_KEY, sessionId);
}

export function getStoredSessionId(): string | null {
  return localStorage.getItem(SESSION_ID_KEY);
}

// ==================== History Parsing Helper ====================

interface RawHistoryMessage {
  role: "user" | "assistant" | "tool";
  content: unknown;
  name?: string;
  tool_call_id?: string;
}

/**
 * Convert a session history payload (from /session/{id}/history) into the
 * frontend's `Message[]` shape. Handles both Anthropic-style structured
 * content arrays and OpenAI-style plain-string content.
 *
 * 后端 session_manager.message_to_dict 现在把两种模式统一为
 * `assistant.content` 始终是块数组，块类型包括：
 *   - { type: "thinking", thinking: string, index: number }
 *   - { type: "text",     text: string[], index: number }
 *   - { type: "tool_use", id?: string, name: string, input: object, index: number }
 *
 * - 数组形态：依次把 thinking / text / tool_use 块转成对应 SSEEvent
 *   （tool_use 生成 tool_call 事件，与 streaming 路径一致）
 * - 字符串形态：兼容老数据，包成单个 text 块（OpenAI 模式 reload 时
 *   如果有 tool_use 块缺失 tool_call UI 的回退）
 *
 * tool 消息始终被合并到紧邻的 assistant 消息的 events 中作为 tool_result。
 */
export function parseHistoryMessages(rawMessages: RawHistoryMessage[]): Message[] {
  const msgs: Message[] = [];

  for (let i = 0; i < rawMessages.length; i++) {
    const msg = rawMessages[i];

    if (msg.role === "user") {
      const isSystemPrompt =
        typeof msg.content === "string" && msg.content.startsWith("[WebSocket]");
      msgs.push({
        id: crypto.randomUUID(),
        role: "user" as const,
        content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
        events: [],
        isComplete: true,
        isSystemPrompt,
      });
    } else if (msg.role === "assistant") {
      const events: SSEEvent[] = [];

      // 1) 数组形态：Anthropic 与 OpenAI（统一后）都走这里
      if (Array.isArray(msg.content)) {
        for (const block of msg.content) {
          if (!block || typeof block !== "object") continue;

          if (block.type === "thinking") {
            events.push({
              type: "thinking" as const,
              content: (block as { thinking?: string }).thinking || "",
              step: (block as { index?: number }).index ?? 0,
            });
          } else if (block.type === "text") {
            events.push({
              type: "text" as const,
              content: (block as { text?: string[] }).text || [],
              step: (block as { index?: number }).index ?? 0,
            });
          } else if (block.type === "tool_use") {
            // tool_use 块 → tool_call 事件（与 streaming 路径一致）。
            // tool_result 由紧随其后的 role="tool" 消息补充。
            events.push({
              type: "tool_call" as const,
              name: (block as { name?: string }).name || "tool",
              input: (block as { input?: Record<string, unknown> }).input || {},
              step: (block as { index?: number }).index ?? 0,
            });
          }
        }
      } else if (typeof msg.content === "string" && msg.content.trim()) {
        // 2) [兼容老数据] 字符串形态：作为单一 text 块
        events.push({
          type: "text" as const,
          content: [msg.content],
          step: events.length > 0 ? (events[events.length - 1].step ?? 0) + 1 : 0,
        });
      }

      // 3) 合并紧随其后的 tool 消息为 tool_result 事件
      let j = i + 1;
      while (j < rawMessages.length && rawMessages[j].role === "tool") {
        events.push({
          type: "tool_result" as const,
          result: (rawMessages[j].content as string) || "",
          step: events.length > 0 ? (events[events.length - 1].step ?? 0) : 0,
        });
        j++;
      }

      msgs.push({
        id: crypto.randomUUID(),
        role: "assistant" as const,
        content: "",
        events,
        isComplete: true,
      });
      i = j - 1; // 跳过已合并的 tool 消息
    }
    // tool 消息已被合并到前一条 assistant，跳过
  }

  return msgs;
}
