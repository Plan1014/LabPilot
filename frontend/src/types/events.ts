export type SSEEventType =
  | "thinking"
  | "tool_call"
  | "tool_result"
  | "text"
  | "ask_user"
  | "auth_request"
  | "done";

export interface SSEEvent {
  type: SSEEventType;
  content?: string | string[];
  name?: string;
  input?: Record<string, unknown>;
  result?: string;
  step: number;
  session_id?: string;
  // `ask_user` 事件的问题正文与候选选项。
  question?: string;
  options?: AskOption[];
  // `auth_request` 事件 —— 判官拿不准，把操作交给用户裁决。
  reason?: string;
  pattern?: string | null;
  batch_id?: string;
  // 裁决结果（补齐 tool_result 时带上）—— 前端据此显示「已允许/已拒绝」。
  auth_choice?: AuthChoice;
  // Populated only on reload from history — not emitted by the live SSE
  // stream. Carries the upstream tool_use id so tool_result can be paired
  // back to the correct tool_call when multiple calls happen in one step.
  tool_call_id?: string;
}

// ==================== Ask-user types ====================

export interface AskOption {
  label: string;
  description?: string;
  recommended: boolean;
}

export interface AskAsk {
  /** 上游 tool_use id —— 回答按它配对 */
  toolCallId: string;
  question: string;
  options: AskOption[];
}

// ==================== 鉴权类型 ====================

/**
 * 审批模式 —— 输入栏左下角的按钮切换。
 *
 * auto：判官自动裁决，拿不准才问用户。
 * manual：判官不下场，命令与写操作一律由用户裁决。
 */
export type AuthzMode = "auto" | "manual";

/** 用户对一个工具调用的裁决 —— 固定三项，顺序不变。 */
export type AuthChoice = "allow_once" | "allow_always" | "deny";

export interface AuthRequest {
  /** 上游 tool_use id —— 裁决按它配对 */
  toolCallId: string;
  /** 给用户看的问题（判官问的，或理由本身） */
  question: string;
  /** 判官给的理由 */
  reason: string;
  /** 「允许并记住」的模式；推不出来时为 null（按钮置灰） */
  pattern: string | null;
  /** 批次 id —— 一轮里的调用是一个原子批次 */
  batchId: string;
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
