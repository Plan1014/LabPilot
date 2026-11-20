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
