import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import Message from "./Message";
import InputArea from "./InputArea";
import HistoryModal from "./HistoryModal";
import MemoryModal from "./MemoryModal";
import AskUserCard from "./AskUserCard";
import { useSSE } from "../hooks/useSSE";
import { useWebSocket } from "../hooks/useWebSocket";
import { getStoredSessionId, persistSessionId, parseHistoryMessages } from "../hooks/useSession";
import {
  deriveAskState,
  shouldDeliverNotification,
  shouldKeepOnStop,
  inputPlaceholder,
} from "../utils/askUser";
import type { Message as MessageType, SessionMeta } from "../types/events";
import { CircleNotch, Clock, Brain } from "@phosphor-icons/react";

const API_BASE = "http://127.0.0.1:8000";

/** 通知的去重键 —— 同一条通知不重复投递给 agent。 */
function notificationKey(notif: {
  source?: string;
  task_id?: string;
  type?: string;
  timestamp?: string;
}): string {
  return [
    notif.source ?? "",
    notif.task_id ?? "",
    notif.type ?? "",
    notif.timestamp ?? "",
  ].join("|");
}

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

  // 提问状态：待答提问、已答对照
  const askState = useMemo(() => deriveAskState(messages), [messages]);

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
            setMessages(parseHistoryMessages(data.messages));
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

  // 已投递过的通知 —— 暂停期间的通知在回答完后再投，不重复也不丢。
  const deliveredNotifications = useRef<Set<string>>(new Set());

  // Show notification as system prompt and notify REPL
  useEffect(() => {
    // [LABPILOT-DEBUG] 通知 useEffect 触发入口
    console.log("[LABPILOT-DEBUG] notification useEffect FIRED", {
      notificationsCount: notifications.length,
      currentStreamingId: streamingMessageId,
      currentIsStreaming: isStreaming,
    });
    if (notifications.length === 0) return;
    // 只发还没发过的通知 —— 否则每次 notifications 数组换新引用都会重发最后一条。
    const undelivered = notifications.filter(
      (n) => !deliveredNotifications.current.has(notificationKey(n))
    );
    // 有待答提问 / 本轮还在跑时不发 —— 判定逻辑见 utils/askUser.ts（有单测）。
    if (
      !shouldDeliverNotification({
        undeliveredCount: undelivered.length,
        hasPendingAsk: askState.pendingAsk !== null,
        isStreaming,
        streamingMessageId,
      })
    ) {
      return;
    }
    const last = undelivered[undelivered.length - 1];
    deliveredNotifications.current.add(notificationKey(last));
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
  }, [notifications, askState.pendingAsk, isStreaming, streamingMessageId]);

  const formatNotification = (notif: { source?: string; type?: string; result?: Record<string, unknown>; timestamp?: string }): string => {    const ts_str = notif.timestamp ? `[${notif.timestamp}] ` : "";
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

  // 点选项作答 —— 直接把选项文本提交；后端会把它当作对上一问的回答。
  const handleAskSelect = useCallback(
    (label: string) => {
      void handleSubmit(label);
    },
    [handleSubmit]
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

    // 2. Delete the streaming assistant message —— 除非它里面有一个
    //    还没被回答的提问：删掉的话提问卡片消失，用户下一句话会被后端
    //    静默当成那个提问的回答。
    if (streamingMessageId) {
      setMessages((prev) =>
        prev
          .filter(
            (msg) =>
              msg.id !== streamingMessageId ||
              shouldKeepOnStop(msg, askState.answers)
          )
          .map((msg) =>
            msg.id === streamingMessageId ? { ...msg, isComplete: true } : msg
          )
      );
    }

    // 3. Delete the user message that triggered the request
    if (lastUserMessageIdRef.current) {
      setMessages((prev) => prev.filter((msg) => msg.id !== lastUserMessageIdRef.current));
      lastUserMessageIdRef.current = null;
    }

    // 4. Reset streaming state
    setIsStreaming(false);
    setStreamingMessageId(null);
  }, [abort, streamingMessageId, askState.answers]);

  const loadSession = useCallback(
    async (id: string) => {
      const res = await fetch(`${API_BASE}/session/${id}/history`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      persistSessionId(id);
      setMessages(parseHistoryMessages(data.messages));
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
          <Message key={msg.id} {...msg} askAnswers={askState.answers} />
        ))}
        <div ref={messagesEndRef} />
      </main>

      {/* 待答提问弹窗 —— 位于输入框上方 */}
      {askState.pendingAsk && (
        <AskUserCard
          ask={askState.pendingAsk}
          onSelect={handleAskSelect}
          disabled={isStreaming}
        />
      )}

      {/* Input */}
      <InputArea
        onSubmit={handleSubmit}
        isStreaming={isStreaming}
        onStop={handleStop}
        placeholder={inputPlaceholder(askState.pendingAsk !== null)}
      />

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
