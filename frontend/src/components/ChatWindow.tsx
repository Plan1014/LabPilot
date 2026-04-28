import { useState, useCallback, useRef, useEffect } from "react";
import Message from "./Message";
import InputArea from "./InputArea";
import { useSSE } from "../hooks/useSSE";
import { useWebSocket } from "../hooks/useWebSocket";
import type { Message as MessageType } from "../types/events";
import { CircleNotch } from "@phosphor-icons/react";

export default function ChatWindow() {
  const [messages, setMessages] = useState<MessageType[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(
    null
  );
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const { events, sendQuery, isConnected, error } = useSSE();

  // WebSocket for notifications
  const { notifications, isConnected: wsConnected } = useWebSocket();

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

      // Add user message
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

      // Add placeholder for assistant
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

      // Send query via SSE
      await sendQuery(query);
    },
    [sendQuery]
  );

  return (
    <div className="flex flex-col h-[100dvh] bg-[#fafafa]">
      {/* Header */}
      <header className="border-b border-[#e5e5e5] bg-white px-4 py-3 flex items-center justify-between">
        <h1 className="text-base font-semibold text-[#1a1a1a]">LabPilot</h1>
        <div className="flex items-center gap-2">
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
    </div>
  );
}
