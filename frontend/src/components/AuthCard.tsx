import { CheckCircle, ShieldCheck, XCircle } from "@phosphor-icons/react";
import type { AuthChoice, AuthRequest } from "../types/events";
import { AUTH_CHOICES, authPayload, canRemember } from "../utils/authCard";

interface Props {
  request: AuthRequest;
  /** 已裁决时显示用户的决定；未裁决时为 undefined */
  decision?: AuthChoice;
  /** 点按钮的回调；未传则为只读态 */
  onDecide?: (choice: AuthChoice) => void;
  disabled?: boolean;
  /**
   * 紧凑态：对话流里的待批调用只显示一行摘要。
   * 可交互的卡片只在输入框上方的弹窗里出现，避免同一件事渲染两遍。
   */
  compact?: boolean;
}

const CHOICE_STYLE: Record<AuthChoice, string> = {
  allow_once: "border-[#e5e5e5] bg-white hover:bg-[#fafafa]",
  allow_always: "border-[#d97757] bg-[#fef7f4] hover:bg-[#fbeae4]",
  deny: "border-[#e5e5e5] bg-white hover:bg-[#fafafa]",
};

const CHOICE_ICON: Record<AuthChoice, typeof CheckCircle> = {
  allow_once: ShieldCheck,
  allow_always: ShieldCheck,
  deny: XCircle,
};

export default function AuthCard({
  request,
  decision,
  onDecide,
  disabled,
  compact,
}: Props) {
  const rememberable = canRemember(request);

  if (decision !== undefined) {
    return (
      <div className="rounded-lg border border-[#e5e5e5] bg-[#f5f5f5] px-3 py-2">
        <div className="flex items-center gap-2 text-xs text-[#666666]">
          <CheckCircle size={14} className="text-[#22c55e]" weight="fill" />
          <span className="font-medium text-[#1a1a1a]">{request.question}</span>
        </div>
        <p className="mt-1 pl-6 text-sm text-[#1a1a1a]">
          已选择：{authPayload(decision)}
        </p>
      </div>
    );
  }

  if (compact) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-[#e5e5e5] bg-[#fafafa] px-3 py-2">
        <ShieldCheck
          size={14}
          className="shrink-0 text-[#d97757]"
          weight="fill"
        />
        <span className="flex-1 text-sm text-[#1a1a1a]">{request.question}</span>
        <span className="shrink-0 rounded-full bg-[#f5f5f5] px-2 py-0.5 text-[10px] text-[#999999]">
          等待批准
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-[#d97757] bg-white px-4 py-3 shadow-sm">
      <div className="flex items-start gap-2">
        <ShieldCheck
          size={16}
          className="mt-0.5 shrink-0 text-[#d97757]"
          weight="fill"
        />
        <p className="text-sm font-medium text-[#1a1a1a]">{request.question}</p>
      </div>

      {request.reason && (
        <p className="mt-1 pl-6 text-xs text-[#999999]">理由：{request.reason}</p>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {AUTH_CHOICES.map((choice) => {
          const Icon = CHOICE_ICON[choice.value];
          const blocked =
            choice.value === "allow_always" && !rememberable;
          return (
            <button
              key={choice.value}
              type="button"
              disabled={disabled || blocked || !onDecide}
              onClick={() => onDecide?.(choice.value)}
              title={blocked ? "这个操作无法归纳成可复用的授权模式" : undefined}
              className={`flex items-center gap-1.5 rounded-lg border px-3 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${CHOICE_STYLE[choice.value]}`}
            >
              <Icon
                size={14}
                className={
                  choice.value === "deny" ? "text-[#999999]" : "text-[#d97757]"
                }
              />
              <span className="text-[#1a1a1a]">{choice.label}</span>
            </button>
          );
        })}
      </div>

      {!rememberable && (
        <p className="mt-2 text-xs text-[#999999]">
          这个操作无法归纳成可复用的授权模式，只能「允许一次」或「拒绝」。
        </p>
      )}

      {disabled && <p className="mt-2 text-xs text-[#999999]">等待本轮结束…</p>}
    </div>
  );
}
