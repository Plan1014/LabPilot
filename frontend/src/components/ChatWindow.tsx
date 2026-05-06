import { useState, useCallback, useRef, useEffect } from "react";
import Message from "./Message";
import InputArea from "./InputArea";
import HistoryModal from "./HistoryModal";
import { useSSE } from "../hooks/useSSE";
import { useWebSocket } from "../hooks/useWebSocket";
import { getStoredSessionId, persistSessionId } from "../hooks/useSession";
import type { Message as MessageType, SessionMeta } from "../types/events";
import { CircleNotch, Clock } from "@phosphor-icons/react";

const API_BASE = "http://127.0.0.1:8000";

export default function ChatWindow() {
  const [messages, setMessages] = useState<MessageType[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { events, sendQuery, isConnected, error } = useSSE();

  // WebSocket for notifications
  const { notifications, isConnected: wsConnected } = useWebSocket();

  // Restore session on mount if there's a stored session_id but no messages yet
  useEffect(() => {
    if (messages.length === 0) {
      const storedId = getStoredSessionId();
      if (storedId) {
        // Try to load the session from backend
        fetch(`${API_BASE}/session/${storedId}/history`)
          .then((res) => {
            if (!res.ok) throw new Error("not found");
            return res.json();
          })
          .then((data) => {
            const msgs: MessageType[] = data.messages.map((msg: any) => ({
              id: crypto.randomUUID(),
              role: msg.role === "user" ? "user" : "assistant",
              content: Array.isArray(msg.content) ? JSON.stringify(msg.content) : msg.content,
              events: [],
              isComplete: true,
            }));
            setMessages(msgs);
          })
          .catch(() => {
            // Session not found in Redis, clear storage
            persistSessionId("");
          });
      }
    }
  }, []); // Only on mount

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Handle SSE events → update streaming message
  useEffect(() => {
    if (!streamingMessageId) return;

    setMessages((prev) =>
      prev.map((msg) =>
        msg.id === streamingMessageId ? { ...msg, events } : msg
      )
    );

    const doneEvent = events.find((e) => e.type === "done");
    if (doneEvent) {
      setMessages((prev) =>
        prev.map((msg) =>
          msg.id === streamingMessageId ? { ...msg, isComplete: true } : msg
        )
      );
      setIsStreaming(false);
      setStreamingMessageId(null);

      // Persist session_id from done event
      if (doneEvent.session_id) {
        persistSessionId(doneEvent.session_id);
      }
    }
  }, [events, streamingMessageId]);

  // Show notification as a system message
  useEffect(() => {
    if (notifications.length === 0) return;
    const last = notifications[notifications.length - 1];
    const notifId = `notif-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      {
        id: notifId,
        role: "assistant",
        content: `[${last.source}] ${last.type}: ${JSON.stringify(last.result || last.error || "")}`,
        events: [],
        isComplete: true,
      },
    ]);
  }, [notifications]);

  const handleSubmit = useCallback(
    async (query: string) => {
      const userMessageId = crypto.randomUUID();
      const assistantMessageId = crypto.randomUUID();

      setMessages((prev) => [
        ...prev,
        {
          id: userMessageId,
          role: "user",
          content: query,
          events: [],
          isComplete: true,
        },
      ]);

      setMessages((prev) => [
        ...prev,
        {
          id: assistantMessageId,
          role: "assistant",
          content: "",
          events: [],
          isComplete: false,
        },
      ]);

      setIsStreaming(true);
      setStreamingMessageId(assistantMessageId);

      const storedId = getStoredSessionId();
      await sendQuery(query, storedId);
    },
    [sendQuery]
  );

  const listSessions = useCallback(async (): Promise<SessionMeta[]> => {
    const res = await fetch(`${API_BASE}/session/list`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data.sessions;
  }, []);

  const loadSession = useCallback(
    async (id: string) => {
      const res = await fetch(`${API_BASE}/session/${id}/history`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      persistSessionId(id);
      const msgs: MessageType[] = data.messages.map((msg: any) => ({
        id: crypto.randomUUID(),
        role: msg.role === "user" ? "user" : "assistant",
        content: Array.isArray(msg.content) ? JSON.stringify(msg.content) : msg.content,
        events: [],
        isComplete: true,
      }));
      setMessages(msgs);
      setShowHistory(false);
    },
    []
  );

  const deleteSession = useCallback(async (id: string) => {
    await fetch(`${API_BASE}/session/${id}`, { method: "DELETE" });
    if (id === getStoredSessionId()) {
      persistSessionId("");
      setMessages([]);
    }
  }, []);

  const handleNewChat = useCallback(() => {
    persistSessionId("");
    setMessages([]);
    setShowHistory(false);
  }, []);

  return (
    <div className="flex flex-col h-[100dvh] bg-[#fafafa]">
      {/* Header */}
      <header className="border-b border-[#e5e5e5] bg-white px-4 py-3 flex items-center justify-between">
        <h1 className="text-base font-semibold text-[#1a1a1a]">LabPilot</h1>
        <div className="flex items-center gap-2">
          <button
            onClick={handleNewChat}
            className="text-xs text-[#666666] hover:text-[#1a1a1a] px-2 py-1 rounded hover:bg-[#f5f5f5] transition-colors"
            title="New chat"
          >
            New
          </button>
          <button
            onClick={() => setShowHistory(true)}
            className="flex items-center gap-1 text-xs text-[#666666] hover:text-[#1a1a1a] px-2 py-1 rounded hover:bg-[#f5f5f5] transition-colors"
          >
            <Clock size={14} />
            History
          </button>
          {/* SSE status */}
          <div className="flex items-center gap-1.5">
            <CircleNotch
              size={12}
              className={isConnected ? "animate-spin text-[#d97757]" : "text-[#d1d5db]"}
            />
            <span className="text-xs text-[#666666]">SSE</span>
          </div>
          {/* WS status */}
          <div className="flex items-center gap-1.5">
            <span
              className={`h-2 w-2 rounded-full ${
                wsConnected ? "bg-[#22c55e]" : "bg-[#d1d5db]"
              }`}
            />
            <span className="text-xs text-[#666666]">WS</span>
          </div>
        </div>
      </header>

      {/* Error banner */}
      {error && (
        <div className="bg-red-50 border-b border-red-200 px-4 py-2">
          <p className="text-xs text-red-600">{error}</p>
        </div>
      )}

      {/* Messages */}
      <main className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <p className="text-sm text-[#999999]">
              Send a message to start a conversation.
            </p>
          </div>
        )}
        {messages.map((msg) => (
          <Message key={msg.id} {...msg} />
        ))}
        <div ref={messagesEndRef} />
      </main>

      {/* Input */}
      <InputArea onSubmit={handleSubmit} isStreaming={isStreaming} />

      {/* History Modal */}
      {showHistory && (
        <HistoryModal
          onClose={() => setShowHistory(false)}
          onLoad={loadSession}
          onDelete={deleteSession}
          listSessions={listSessions}
        />
      )}
    </div>
  );
}
