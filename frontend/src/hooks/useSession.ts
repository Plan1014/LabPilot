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

      // Convert history to Message format
      // Tool results are stored as separate "tool" messages after assistant,
      // need to merge them together
      const msgs: Message[] = [];
      const messages = data.messages;

      for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];

        if (msg.role === "user") {
          msgs.push({
            id: crypto.randomUUID(),
            role: "user" as const,
            content: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content),
            events: [],
            isComplete: true,
          });
        } else if (msg.role === "assistant") {
          const events: SSEEvent[] = [];
          const contentArray = Array.isArray(msg.content) ? msg.content : [];

          for (const block of contentArray) {
            if (!block || typeof block !== "object") continue;

            if (block.type === "thinking") {
              events.push({
                type: "thinking" as const,
                content: block.thinking || "",
                step: block.index ?? 0,
              });
            } else if (block.type === "text") {
              events.push({
                type: "text" as const,
                content: block.text || [],
                step: block.index ?? 0,
              });
            }
            // Note: tool_use blocks are NOT converted to tool_result here
            // The actual tool results come from following "tool" messages
          }

          // Check if next messages are tool results
          let j = i + 1;
          while (j < messages.length && messages[j].role === "tool") {
            events.push({
              type: "tool_result" as const,
              result: messages[j].content || "",
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
          i = j - 1; // Skip processed tool messages
        }
        // Skip tool messages (already processed with assistant)
      }
      setMessages(msgs);
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
