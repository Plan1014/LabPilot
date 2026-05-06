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

      // Convert history to Message format
      const msgs: Message[] = data.messages.map((msg: any) => ({
        id: crypto.randomUUID(),
        role: msg.role === "user" ? "user" : "assistant",
        content: Array.isArray(msg.content) ? JSON.stringify(msg.content) : msg.content,
        events: [],
        isComplete: true,
      }));
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
