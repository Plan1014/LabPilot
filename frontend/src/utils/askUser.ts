import type { AskAsk, AskOption, Message, SSEEvent } from "../types/events";

/**
 * 把模型给的原始 options 归一化成固定形状。
 *
 * 与后端 src/agent/ask_user.py 的 normalize_options 保持一致：
 * label 必须非空；description 缺失即 undefined；recommended 只认 true。
 */
export function normalizeOptions(raw: unknown): AskOption[] {
  if (!Array.isArray(raw)) return [];
  const out: AskOption[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const o = item as {
      label?: unknown;
      description?: unknown;
      recommended?: unknown;
    };
    if (typeof o.label !== "string" || !o.label.trim()) continue;
    out.push({
      label: o.label.trim(),
      description:
        typeof o.description === "string" && o.description.trim()
          ? o.description.trim()
          : undefined,
      recommended: o.recommended === true,
    });
  }
  return out.slice(0, 4);
}

/** 把 recommended 的选项排到第一位；没有 recommended 时保持原序。 */
export function orderOptions(options: AskOption[]): AskOption[] {
  const idx = options.findIndex((o) => o.recommended);
  if (idx <= 0) return options;
  const copy = [...options];
  const [pick] = copy.splice(idx, 1);
  return [pick, ...copy];
}

function askFromEvent(ev: SSEEvent): AskAsk | null {
  if (ev.type === "ask_user") {
    return {
      toolCallId: ev.tool_call_id ?? "",
      question: typeof ev.question === "string" ? ev.question : "",
      options: normalizeOptions(ev.options),
    };
  }
  if (ev.type === "tool_call" && ev.name === "ask_user") {
    const input = (ev.input ?? {}) as { question?: unknown; options?: unknown };
    return {
      toolCallId: ev.tool_call_id ?? "",
      question: typeof input.question === "string" ? input.question : "",
      options: normalizeOptions(input.options),
    };
  }
  return null;
}

/**
 * 从整个消息流里推导提问状态。
 *
 * 配对只按 tool_call_id —— 同一个问题被问两次时 id 不同，不能靠文本配对。
 */
export function deriveAskState(messages: Message[]): {
  asks: AskAsk[];
  answers: Map<string, string>;
  pendingAsk: AskAsk | null;
} {
  const asks: AskAsk[] = [];
  const answers = new Map<string, string>();
  for (const msg of messages ?? []) {
    for (const ev of msg.events ?? []) {
      const ask = askFromEvent(ev);
      if (ask) {
        asks.push(ask);
        continue;
      }
      if (ev.type === "tool_result" && ev.tool_call_id) {
        answers.set(ev.tool_call_id, ev.result ?? "");
      }
    }
  }
  const pendingAsk =
    [...asks].reverse().find((a) => !answers.has(a.toolCallId)) ?? null;
  return { asks, answers, pendingAsk };
}

/**
 * 通知该不该现在投给 agent。
 *
 * 四种情况一律不投：
 * - 没有未投递的通知（都已投过，别重复）
 * - 有待答提问 —— 通知文本会被后端当成「对上一问的回答」吞掉
 * - 有待批的鉴权操作 —— 通知文本会被后端当成「直接敲字」，最早的待批
 *   项会被判成拒绝（后端不解析自由文本，猜用户的意思比拒绝更危险）
 * - 本轮还在跑 —— sendQuery 会 abort 掉正在跑的流，通知会把这一轮打断
 *
 * 抽成纯函数是为了能单测：这段逻辑原本内联在 ChatWindow 的 useEffect 里，
 * 一旦改错只有手动点一遍才能发现。
 */
export function shouldDeliverNotification(state: {
  /** 还没投过的通知条数 */
  undeliveredCount: number;
  /** 是否有待答提问 */
  hasPendingAsk: boolean;
  /** 是否有待批的鉴权操作 */
  hasPendingAuth?: boolean;
  /** 当前是否在流式中 */
  isStreaming: boolean;
  /** 当前流式消息 id（未开始时为 null） */
  streamingMessageId: string | null;
}): boolean {
  if (state.undeliveredCount <= 0) return false;
  if (state.hasPendingAsk) return false;
  if (state.hasPendingAuth) return false;
  if (state.isStreaming || state.streamingMessageId) return false;
  return true;
}

/**
 * 停止本轮时，这条消息要不要留在对话里。
 *
 * 消息里若有一个还没被回答的提问，就得留着 —— 删掉的话前端不再提示「在等
 * 回答」，而后端那边提问还挂着，用户下一句话会被静默当成回答。
 *
 * 同理，还没裁决的鉴权请求也得留着 —— 后端那一批还挂着。
 */
export function shouldKeepOnStop(
  message: Message,
  answers: Map<string, string>,
  authDecisions?: Map<string, string>
): boolean {
  for (const ev of message.events ?? []) {
    const ask = askFromEvent(ev);
    if (ask && !answers.has(ask.toolCallId)) return true;
    if (ev.type === "auth_request" && ev.tool_call_id) {
      if (!authDecisions?.has(ev.tool_call_id)) return true;
    }
  }
  return false;
}

/** 输入框的占位文案 —— 有待答提问时要让用户知道下一句是「回答」。 */
export function inputPlaceholder(hasPendingAsk: boolean): string {
  return hasPendingAsk ? "回答上面的提问…" : "Ask LabPilot...";
}
