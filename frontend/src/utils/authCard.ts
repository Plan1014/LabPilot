import type { AuthChoice, AuthRequest, Message, SSEEvent } from "../types/events";

/**
 * 卡片上的三个固定选项 —— 顺序不变，用户每次看到的都一样。
 *
 * 「允许并记住」是用户授权出来的：模式由后端确定性推导，推不出来时这一项
 * 置灰（见 canRemember）。
 */
export const AUTH_CHOICES: { value: AuthChoice; label: string }[] = [
  { value: "allow_once", label: "允许一次" },
  { value: "allow_always", label: "允许并记住" },
  { value: "deny", label: "拒绝" },
];

/** 「允许并记住」有没有得记 —— 没有模式就无从「记住」。 */
export function canRemember(req: AuthRequest): boolean {
  return typeof req.pattern === "string" && req.pattern.trim().length > 0;
}

/** 决定的中文说明 —— 卡片上「已选择：…」那行。 */
export function authPayload(choice: AuthChoice): string {
  const found = AUTH_CHOICES.find((c) => c.value === choice);
  return found ? found.label : choice;
}

/**
 * 输入框的占位文案 —— 有待批操作时要让用户知道直接敲字会被当成拒绝。
 */
export function authPlaceholder(hasPendingAuth: boolean): string {
  return hasPendingAuth ? "先在上面的卡片上选择，或直接输入（将视为拒绝）…" : "";
}

function authFromEvent(ev: SSEEvent): AuthRequest | null {
  if (ev.type !== "auth_request") return null;
  return {
    toolCallId: ev.tool_call_id ?? "",
    question: typeof ev.question === "string" ? ev.question : "",
    reason: typeof ev.reason === "string" ? ev.reason : "",
    pattern: typeof ev.pattern === "string" ? ev.pattern : null,
    batchId: ev.batch_id ?? "",
  };
}

/** 没有 auth_choice 时的兜底推断 —— 拒绝说明里带「授权拒绝」四个字。 */
function inferChoice(result: string | undefined): AuthChoice {
  return (result ?? "").startsWith("授权拒绝") ? "deny" : "allow_once";
}

/**
 * 从整个消息流里推导鉴权卡片状态。
 *
 * 配对只按 tool_call_id —— 后端补齐 tool_result 时带着同一个 id，出现即
 * 视为已裁决。跟 deriveAskState 同一套规矩。
 */
export function deriveAuthState(messages: Message[]): {
  requests: AuthRequest[];
  decisions: Map<string, AuthChoice>;
  pending: AuthRequest | null;
} {
  const requests: AuthRequest[] = [];
  const decisions = new Map<string, AuthChoice>();
  for (const msg of messages ?? []) {
    for (const ev of msg.events ?? []) {
      const req = authFromEvent(ev);
      if (req) {
        requests.push(req);
        continue;
      }
      if (ev.type === "tool_result" && ev.tool_call_id) {
        decisions.set(ev.tool_call_id, ev.auth_choice ?? inferChoice(ev.result));
      }
    }
  }
  const pending =
    [...requests].reverse().find((r) => !decisions.has(r.toolCallId)) ?? null;
  return { requests, decisions, pending };
}

/**
 * 把 GET /auth/pending 的返回转成待批卡片 —— 刷新页面后重建卡片用。
 *
 * 畸形输入一律返回空数组：宁可卡片不显示，也不能让前端崩掉。
 */
export function authRequestsFromPending(raw: unknown): AuthRequest[] {
  if (!raw || typeof raw !== "object") return [];
  const list = (raw as { pending?: unknown }).pending;
  if (!Array.isArray(list)) return [];
  const out: AuthRequest[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    const toolCallId = typeof o.tool_call_id === "string" ? o.tool_call_id : "";
    if (!toolCallId) continue;
    out.push({
      toolCallId,
      question: typeof o.question === "string" ? o.question : "",
      reason: typeof o.reason === "string" ? o.reason : "",
      pattern: typeof o.pattern === "string" ? o.pattern : null,
      batchId: typeof o.batch_id === "string" ? o.batch_id : "",
    });
  }
  return out;
}
