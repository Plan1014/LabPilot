import { CheckCircle, Lightbulb } from "@phosphor-icons/react";
import type { AskAsk } from "../types/events";
import { orderOptions } from "../utils/askUser";

interface Props {
  ask: AskAsk;
  /** 已作答时显示答案；未作答时为 undefined */
  answer?: string;
  /** 点击选项的回调；未传则为只读态 */
  onSelect?: (label: string) => void;
  disabled?: boolean;
  /**
   * 紧凑态：对话流里的未答提问只显示一行摘要。
   * 可交互的提问卡片只在输入框上方的弹窗里出现，避免同一问题渲染两遍。
   */
  compact?: boolean;
}

export default function AskUserCard({
  ask,
  answer,
  onSelect,
  disabled,
  compact,
}: Props) {
  const answered = answer !== undefined;

  if (answered) {
    return (
      <div className="rounded-lg border border-[#e5e5e5] bg-[#f5f5f5] px-3 py-2">
        <div className="flex items-center gap-2 text-xs text-[#666666]">
          <CheckCircle size={14} className="text-[#22c55e]" weight="fill" />
          <span className="font-medium text-[#1a1a1a]">{ask.question}</span>
        </div>
        <p className="mt-1 pl-6 text-sm text-[#1a1a1a]">已选择：{answer}</p>
      </div>
    );
  }

  if (compact) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-[#e5e5e5] bg-[#fafafa] px-3 py-2">
        <Lightbulb size={14} className="shrink-0 text-[#d97757]" weight="fill" />
        <span className="flex-1 text-sm text-[#1a1a1a]">{ask.question}</span>
        <span className="shrink-0 rounded-full bg-[#f5f5f5] px-2 py-0.5 text-[10px] text-[#999999]">
          等待回答
        </span>
      </div>
    );
  }

  // 模型标了 recommended 的选项排到第一位；没有标则保持原序。
  // 徽标统一给第一项 —— 用户看到的第一条就是推荐的那条。
  const options = orderOptions(ask.options);

  return (
    <div className="rounded-lg border border-[#d97757] bg-white px-4 py-3 shadow-sm">
      <div className="flex items-start gap-2">
        <Lightbulb
          size={16}
          className="mt-0.5 shrink-0 text-[#d97757]"
          weight="fill"
        />
        <p className="text-sm font-medium text-[#1a1a1a]">{ask.question}</p>
      </div>

      {options.length > 0 ? (
        <div className="mt-3 flex flex-col gap-2">
          {options.map((opt, idx) => (
            <button
              key={opt.label}
              type="button"
              disabled={disabled || !onSelect}
              onClick={() => onSelect?.(opt.label)}
              className={`rounded-lg border px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                idx === 0
                  ? "border-[#d97757] bg-[#fef7f4] hover:bg-[#fbeae4]"
                  : "border-[#e5e5e5] bg-white hover:bg-[#fafafa]"
              }`}
            >
              <div className="flex items-center gap-2">
                <span className="text-sm text-[#1a1a1a]">{opt.label}</span>
                {idx === 0 && (
                  <span className="rounded-full bg-[#d97757] px-2 py-0.5 text-[10px] font-medium text-white">
                    推荐
                  </span>
                )}
              </div>
              {opt.description && (
                <p className="mt-0.5 text-xs text-[#999999]">{opt.description}</p>
              )}
            </button>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-xs text-[#999999]">请直接在下方输入框作答</p>
      )}

      {disabled && (
        <p className="mt-2 text-xs text-[#999999]">等待本轮结束…</p>
      )}

      {options.length > 0 && (
        <p className="mt-2 text-xs text-[#999999]">或直接在下方输入框作答</p>
      )}
    </div>
  );
}
