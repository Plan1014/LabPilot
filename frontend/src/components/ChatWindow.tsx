import { useState, useCallback, useRef, useEffect } from "react";
import Message from "./Message";
import InputArea from "./InputArea";
import HistoryModal from "./HistoryModal";
import MemoryModal from "./MemoryModal";
import { useSSE } from "../hooks/useSSE";
import { useWebSocket } from "../hooks/useWebSocket";
import { getStoredSessionId, persistSessionId } from "../hooks/useSession";
import type { Message as MessageType, SessionMeta, SSEEvent } from "../types/events";
import { CircleNotch, Clock, Brain } from "@phosphor-icons/react";

const API_BASE = "http://127.0.0.1:8000";

export default function ChatWindow() {
  const [messages, setMessages] = useState<MessageType[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { events, sendQuery, isConnected, error, abort } = useSSE();

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
            // Merge tool messages with their preceding assistant message
            const msgs: MessageType[] = [];
            const messages = data.messages;

            for (let i = 0; i < messages.length; i++) {
              const msg = messages[i];

              if (msg.role === "user") {
                const isSystemPrompt = typeof msg.content === "string" && msg.content.startsWith("[WebSocket]");
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
                i = j - 1;
              }
            }
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

  // Show notification as system prompt and notify REPL
  useEffect(() => {
    // [LABPILOT-DEBUG] 通知 useEffect 触发入口
    console.log("[LABPILOT-DEBUG] notification useEffect FIRED", {
      notificationsCount: notifications.length,
      currentStreamingId: streamingMessageId,
      currentIsStreaming: isStreaming,
    });
    if (notifications.length === 0) return;
    const last = notifications[notifications.length - 1];
    console.log("[LABPILOT-DEBUG] notification PAYLOAD", last);
    const formatted = formatNotification(last);

    // Add system prompt message (this is the query sent to REPL)
    const userMessageId = `user-${Date.now()}`;
    const assistantMessageId = `assistant-${Date.now()}`;

    setMessages((prev) => [
      ...prev,
      {
        id: userMessageId,
        role: "user",
        content: formatted,
        events: [],
        isComplete: true,
        isSystemPrompt: true,  // Mark as system prompt for gray card rendering
      },
    ]);

    setIsStreaming(true);
    setStreamingMessageId(assistantMessageId);

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

    const storedId = getStoredSessionId();
    // [LABPILOT-DEBUG] 关键：调用 sendQuery 前的 storedId 和 formatted
    console.log("[LABPILOT-DEBUG] notification ABOUT TO sendQuery", {
      storedId,
      formatted,
    });
    sendQuery(formatted, storedId);
  }, [notifications]);

  const formatNotification = (notif: { source?: string; type?: string; result?: Record<string, unknown>; timestamp?: string }): string => {
    const ts_str = notif.timestamp ? `[${notif.timestamp}] ` : "";
    const source_str = notif.source ? `[${notif.source}] ` : "";
    const result = notif.result || {};

    let content = "";
    if (notif.type === "task_completed") {
      const parts: string[] = [];
      for (const [k, v] of Object.entries(result)) {
        parts.push(`${k}=${v}`);
      }
      content = parts.join(", ");
    } else {
      content = JSON.stringify(result);
    }

    return `[WebSocket] ${ts_str}${source_str}${notif.type}: ${content}`;
  };

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

      lastUserMessageIdRef.current = userMessageId;

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
    [abort, sendQuery]
  );

  const listSessions = useCallback(async (): Promise<SessionMeta[]> => {
    const res = await fetch(`${API_BASE}/session/list`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data.sessions;
  }, []);

  // Track IDs for stop functionality
  const lastUserMessageIdRef = useRef<string | null>(null);

  const handleStop = useCallback(() => {
    // 1. Abort SSE request
    abort();

    // 2. Delete the streaming assistant message
    if (streamingMessageId) {
      setMessages((prev) => prev.filter((msg) => msg.id !== streamingMessageId));
    }

    // 3. Delete the user message that triggered the request
    if (lastUserMessageIdRef.current) {
      setMessages((prev) => prev.filter((msg) => msg.id !== lastUserMessageIdRef.current));
      lastUserMessageIdRef.current = null;
    }

    // 4. Reset streaming state
    setIsStreaming(false);
    setStreamingMessageId(null);
  }, [abort, streamingMessageId]);

  const loadSession = useCallback(
    async (id: string) => {
      const res = await fetch(`${API_BASE}/session/${id}/history`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      persistSessionId(id);
      // Merge tool messages with their preceding assistant message
      const msgs: MessageType[] = [];
      const messages = data.messages;

      for (let i = 0; i < messages.length; i++) {
        const msg = messages[i];

        if (msg.role === "user") {
          const isSystemPrompt = typeof msg.content === "string" && msg.content.startsWith("[WebSocket]");
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
            // Note: tool_use blocks are NOT converted here
            // Actual tool results come from following "tool" messages
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
        // Skip tool messages (already merged)
      }
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
          <button
            onClick={() => setShowMemory(true)}
            className="flex items-center gap-1 text-xs text-[#666666] hover:text-[#1a1a1a] px-2 py-1 rounded hover:bg-[#f5f5f5] transition-colors"
            title="Memory Panel"
          >
            <Brain size={14} />
            Memory
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
      <InputArea onSubmit={handleSubmit} isStreaming={isStreaming} onStop={handleStop} />

      {/* History Modal */}
      {showHistory && (
        <HistoryModal
          onClose={() => setShowHistory(false)}
          onLoad={loadSession}
          onDelete={deleteSession}
          listSessions={listSessions}
        />
      )}

      {/* Memory Modal */}
      {showMemory && (
        <MemoryModal onClose={() => setShowMemory(false)} />
      )}
    </div>
  );
}
