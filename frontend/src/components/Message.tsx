import React from "react";
import ThinkingBlock from "./ThinkingBlock";
import ToolResultBlock from "./ToolResultBlock";
import TextBlock from "./TextBlock";
import type { SSEEvent } from "../types/events";

interface Props {
  role: "user" | "assistant";
  content: string;
  events: SSEEvent[];
  isComplete: boolean;
}

// Sort order within a step: thinking=0, tool_call=1, tool_result=2, text=3
const TYPE_ORDER: Record<string, number> = {
  thinking: 0,
  tool_call: 1,
  tool_result: 2,
  text: 3,
};

export default function Message({ role, content, events, isComplete }: Props) {
  const isUser = role === "user";

  // Sort by step, then by type order
  const sortedEvents = [...events].sort((a, b) => {
    const stepDiff = (a.step ?? 0) - (b.step ?? 0);
    if (stepDiff !== 0) return stepDiff;
    return (TYPE_ORDER[a.type] ?? 99) - (TYPE_ORDER[b.type] ?? 99);
  });

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
      renderBlocks.push(
        <ToolResultBlock
          key={`result-${i}`}
          type="result"
          result={ev.result}
          step={ev.step}
        />
      );
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
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-lg px-4 py-3 ${
          isUser
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
