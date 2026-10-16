import { useState, useCallback } from "react";
import type { SessionMeta, Message } from "../types/events";

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

  // One user query triggers a full agent loop. The loop produces N AIMessages
  // (thinking → tool_call → tool_call → ... → text) plus M ToolMessages in
  // LangChain's storage, all persisted as separate rows. From the user's
  // perspective, those N+M rows are ONE answer to their question and should
  // render as ONE message card, not N+M tiny cards.
  //
  // Strategy: walk the rows in order. Push a user row as its own card. For
  // assistant+tool rows, APPEND events to the last in-progress assistant
  // card (creating one if needed). Move on to the next user row only when we
  // actually see one — that user's question is the natural boundary.
  let currentAssistant: Message | null = null;
  let lastStep = 0; // monotonic step counter across the whole agent loop

  const startAssistant = (): Message => {
    const m: Message = {
      id: crypto.randomUUID(),
      role: "assistant" as const,
      content: "",
      events: [],
      isComplete: true,
    };
    msgs.push(m);
    return m;
  };

  const appendBlockToAssistant = (block: { type?: string; [k: string]: unknown }) => {
    if (!currentAssistant) currentAssistant = startAssistant();

    if (block.type === "thinking") {
      currentAssistant.events.push({
        type: "thinking" as const,
        content: (block as { thinking?: string }).thinking || "",
        step: lastStep++,
      });
    } else if (block.type === "text") {
      currentAssistant.events.push({
        type: "text" as const,
        content: (block as { text?: string[] }).text || [],
        step: lastStep++,
      });
    } else if (block.type === "tool_use") {
      // tool_use 块 → tool_call 事件（与 streaming 路径一致）。
      //
      // `input` may be empty `{}` when the assistant message was captured
      // mid-stream: the langchain_anthropic client parks the live JSON in
      // `partial_json` until the tool_use block closes, then `input` is filled
      // in. Our DB snapshot can land in either state, so prefer `input` and
      // fall back to a JSON.parse(partial_json). Without this fallback the
      // reload view shows every tool call as `Input: {}`.
      let toolInput: Record<string, unknown> =
        (block as { input?: Record<string, unknown> }).input || {};
      if (
        Object.keys(toolInput).length === 0 &&
        typeof (block as { partial_json?: unknown }).partial_json === "string"
      ) {
        try {
          const parsed = JSON.parse(
            (block as { partial_json: string }).partial_json
          );
          if (parsed && typeof parsed === "object") {
            toolInput = parsed as Record<string, unknown>;
          }
        } catch {
          // partial_json was truncated mid-write — leave input empty.
        }
      }
      // Record the step we hand to tool_call so the matching tool_result
      // (arrives later as a role="tool" row) can reuse it. The user reads
      // "[Step 1] bash" + "[Step 1] Result" as ONE call, not two — sharing
      // the step number makes that pairing visually obvious.
      currentAssistant.events.push({
        type: "tool_call" as const,
        name: (block as { name?: string }).name || "tool",
        input: toolInput,
        step: lastStep,
        tool_call_id: (block as { id?: string }).id,
      });
      // Bump AFTER the tool_call lands. This way a single iteration that
      // emits two parallel tool_uses (e.g. head + tail in one response)
      // gets distinct steps 3 and 4, and their respective tool_results
      // (matched by tool_call_id) also display at 3 and 4.
      lastStep++;
    }
  };

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
      // Next assistant/tool rows belong to this user — reset the per-loop
      // step counter so the card's [Step N] labels restart at 1.
      currentAssistant = null;
      lastStep = 0;
      continue;
    }

    if (msg.role === "assistant") {
      if (Array.isArray(msg.content)) {
        for (const block of msg.content) {
          if (!block || typeof block !== "object") continue;
          appendBlockToAssistant(block);
        }
      } else if (typeof msg.content === "string" && msg.content.trim()) {
        // 兼容老数据：字符串形态 → 单一 text 块
        if (!currentAssistant) currentAssistant = startAssistant();
        currentAssistant.events.push({
          type: "text" as const,
          content: [msg.content],
          step: lastStep++,
        });
      }
      continue;
    }

    if (msg.role === "tool") {
      if (!currentAssistant) currentAssistant = startAssistant();
      // Pair with the tool_call that has the matching tool_call_id. Without
      // the id, the safe fallback is "the most recent tool_call" — but that
      // breaks when a single agent iteration emits multiple parallel calls
      // (e.g. head + tail in one response), since the LAST call's step
      // would steal the earlier result.
      const toolCallId = (msg as { tool_call_id?: string }).tool_call_id;
      let matchedStep: number | null = null;
      if (toolCallId) {
        for (const ev of currentAssistant.events) {
          if (ev.type === "tool_call" && ev.tool_call_id === toolCallId) {
            matchedStep = ev.step ?? null;
            break;
          }
        }
      }
      if (matchedStep === null) {
        // Fallback for legacy rows that pre-date the id-tracking fix: pick
        // the last tool_call in the card. Better than no match.
        for (let k = currentAssistant.events.length - 1; k >= 0; k--) {
          const ev = currentAssistant.events[k];
          if (ev.type === "tool_call") {
            matchedStep = ev.step ?? null;
            break;
          }
        }
      }
      currentAssistant.events.push({
        type: "tool_result" as const,
        result: (msg.content as string) || "",
        step: matchedStep ?? lastStep,
      });
      // Do NOT advance lastStep here — the next event is either the
      // matching tool_result's pair (already at this step) or the next
      // tool_call/text which tool_call already advanced.
      continue;
    }
  }

  return msgs;
}
