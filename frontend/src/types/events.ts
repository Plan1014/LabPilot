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
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  events: SSEEvent[];
  isComplete: boolean;
}

export interface Notification {
  source: string;
  task_id: string;
  type: string;
  result?: Record<string, unknown>;
  error?: string;
  timestamp: string;
}
