import React from "react";
import ThinkingBlock from "./ThinkingBlock";
import ToolResultBlock from "./ToolResultBlock";
import TextBlock from "./TextBlock";
import AskUserCard from "./AskUserCard";
import type { SSEEvent } from "../types/events";
import { deriveAskState } from "../utils/askUser";

interface Props {
  role: "user" | "assistant";
  content: string;
  events: SSEEvent[];
  isComplete: boolean;
  isSystemPrompt?: boolean;
  /** tool_call_id -> 回答文本（来自整个会话流） */
  askAnswers?: Map<string, string>;
}

// Sort order within a step: thinking=0, tool_call=1, tool_result=2, text=3
const TYPE_ORDER: Record<string, number> = {
  thinking: 0,
  tool_call: 1,
  tool_result: 2,
  text: 3,
  ask_user: 4,
};

export default function Message({
  role,
  content,
  events,
  isComplete,
  isSystemPrompt,
  askAnswers,
}: Props) {
  const isUser = role === "user";

  // Sort by step, then by type order
  const sortedEvents = [...events].sort((a, b) => {
    const stepDiff = (a.step ?? 0) - (b.step ?? 0);
    if (stepDiff !== 0) return stepDiff;
    return (TYPE_ORDER[a.type] ?? 99) - (TYPE_ORDER[b.type] ?? 99);
  });

  // 本条消息里的提问；回答文本从整会话的 askAnswers 里取
  // （回答可能落在后一条消息的 tool_result 事件里）。
  const { asks, answers: localAnswers } = deriveAskState([
    {
      id: "self",
      role: "assistant",
      content: "",
      events: sortedEvents,
      isComplete: true,
    },
  ]);
  const answerOf = (toolCallId: string): string | undefined => {
    const fromAll = askAnswers?.get(toolCallId);
    if (fromAll !== undefined) return fromAll;
    return localAnswers.get(toolCallId);
  };

  // Group consecutive thinking events for ThinkingBlock
  const renderBlocks: React.ReactNode[] = [];
  let i = 0;
  while (i < sortedEvents.length) {
    const ev = sortedEvents[i];
    if (ev.type === "thinking") {
      // Collect all consecutive thinking events
      const thinkingGroup: string[] = [];
      while (i < sortedEvents.length && sortedEvents[i].type === "thinking") {
        if (sortedEvents[i].content) {
          thinkingGroup.push(sortedEvents[i].content as string);
        }
        i++;
      }
      renderBlocks.push(<ThinkingBlock key={`thinking-${i}`} blocks={thinkingGroup} />);
    } else if (ev.type === "ask_user") {
      // 提问 —— 对话流里只显示紧凑摘要，可交互卡片在上方弹窗里
      const ask = asks.find((a) => a.toolCallId === ev.tool_call_id);
      renderBlocks.push(
        <AskUserCard
          key={`ask-${i}`}
          ask={ask ?? { toolCallId: ev.tool_call_id ?? "", question: ev.question ?? "", options: [] }}
          answer={answerOf(ev.tool_call_id ?? "")}
          compact
        />
      );
      i++;
    } else if (ev.type === "tool_call" && ev.name === "ask_user") {
      // 回放路径：tool_use 块被解析成 tool_call 事件
      const ask = asks.find((a) => a.toolCallId === ev.tool_call_id);
      renderBlocks.push(
        <AskUserCard
          key={`ask-${i}`}
          ask={ask ?? { toolCallId: ev.tool_call_id ?? "", question: "", options: [] }}
          answer={answerOf(ev.tool_call_id ?? "")}
          compact
        />
      );
      i++;
    } else if (ev.type === "tool_call") {
      renderBlocks.push(
        <ToolResultBlock
          key={`call-${i}`}
          type="call"
          name={ev.name}
          input={ev.input}
          step={ev.step}
        />
      );
      i++;
    } else if (ev.type === "tool_result") {
      // 提问的回答已经在 AskUserCard 的已答态里呈现了，不再重复渲染
      const isAskAnswer =
        ev.name === "ask_user" &&
        ev.tool_call_id !== undefined &&
        answerOf(ev.tool_call_id) !== undefined;
      if (!isAskAnswer) {
        renderBlocks.push(
          <ToolResultBlock
            key={`result-${i}`}
            type="result"
            result={ev.result}
            step={ev.step}
          />
        );
      }
      i++;
    } else if (ev.type === "text") {
      renderBlocks.push(
        <TextBlock key={`text-${i}`} content={ev.content as string[]} />
      );
      i++;
    } else {
      i++;
    }
  }

  return (
    <div className={`flex ${isUser && !isSystemPrompt ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-lg px-4 py-3 ${
          isUser && isSystemPrompt
            ? "bg-[#f5f5f5] border border-[#e0e0e0]"
            : isUser
            ? "bg-[#d97757] text-white"
            : "bg-white border border-[#e5e5e5]"
        }`}
      >
        {isUser ? (
          <p className="text-sm">{content}</p>
        ) : (
          <div className="space-y-3">
            {renderBlocks}
            {!isComplete && (
              <div className="flex items-center gap-2 text-[#999999] text-xs">
                <span className="h-2 w-2 rounded-full bg-[#d97757] animate-pulse" />
                <span>Generating...</span>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
