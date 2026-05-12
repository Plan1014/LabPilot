export type SSEEventType =
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "text"
  | "done";

export interface SSEEvent {
  type: SSEEventType;
  content?: string | string[];
  name?: string;
  input?: Record<string, unknown>;
  result?: string;
  step: number;
  session_id?: string;
}

export interface SessionMeta {
  id: string;
  title: string;
  created_at: string;
  last_message_at: string;
  message_count: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  events: SSEEvent[];
  isComplete: boolean;
  isSystemPrompt?: boolean;
}

export interface Notification {
  source: string;
  task_id: string;
  type: string;
  result?: Record<string, unknown>;
  error?: string;
  timestamp: string;
}

// ==================== Memory types ====================

export interface MemoryFact {
  doc_id: string;
  content: string;
  timestamp: number;
  source: string;
}

export interface MemorySummary {
  doc_id: string;
  content: string;
  timestamp: number;
  filepath: string;
}

export interface MemoryPage<T> {
  total: number;
  items: T[];
}

export interface MemoryBlock {
  label: string;
  value: string;
  description: string;
  limit: number;
  read_only: boolean;
  created_at: number;
  updated_at: number;
}
