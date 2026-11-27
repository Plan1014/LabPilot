import React, { useState } from "react";
import { PaperPlaneTilt, StopCircle } from "@phosphor-icons/react";
import type { AuthzMode } from "../types/events";
import { modeDescription, modeLabel } from "../utils/authzMode";

interface Props {
  onSubmit: (query: string) => void;
  isStreaming: boolean;
  onStop?: () => void;
  /** 输入框占位文案 */
  placeholder?: string;
  /** 当前审批模式（auto / manual） */
  authzMode: AuthzMode;
  /** 点左下角的模式按钮 */
  onToggleAuthzMode: () => void;
}

export default function InputArea({
  onSubmit,
  isStreaming,
  onStop,
  placeholder = "Ask LabPilot...",
  authzMode,
  onToggleAuthzMode,
}: Props) {
  const [input, setInput] = useState("");
  const [showModeInfo, setShowModeInfo] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (input.trim() && !isStreaming) {
      onSubmit(input.trim());
      setInput("");
    }
  };

  const handleClick = () => {
    if (isStreaming && onStop) {
      onStop();
    } else if (!isStreaming && input.trim()) {
      onSubmit(input.trim());
      setInput("");
    }
  };

  // 切模式的同时展开那行说明 —— 用户得知道自己在哪个模式里。
  const handleToggleMode = () => {
    onToggleAuthzMode();
    setShowModeInfo(true);
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="border-t border-[#e5e5e5] bg-white p-4"
    >
      <div className="flex items-center gap-3">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={placeholder}
          disabled={isStreaming}
          className="flex-1 px-4 py-2 border border-[#d1d5db] rounded-lg text-sm
                     focus:outline-none focus:border-[#d97757] focus:ring-1 focus:ring-[#d97757]
                     disabled:bg-[#f5f5f5] disabled:text-[#999999]
                     placeholder:text-[#9ca3af]"
        />
        <button
          type="button"
          onClick={handleClick}
          className="p-2 bg-[#d97757] text-white rounded-lg hover:bg-[#c4684a]
                     disabled:bg-[#e5e5e5] disabled:text-[#999999] transition-colors"
        >
          {isStreaming ? (
            <StopCircle size={20} weight="regular" />
          ) : (
            <PaperPlaneTilt size={20} weight="regular" />
          )}
        </button>
      </div>

      {/* 左下角：审批模式切换 —— 按钮上写的就是当前模式 */}
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          onClick={handleToggleMode}
          title={modeDescription(authzMode)}
          className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium transition-colors ${
            authzMode === "manual"
              ? "border-[#d97757] bg-[#fef7f4] text-[#d97757]"
              : "border-[#e5e5e5] bg-[#f5f5f5] text-[#666666]"
          }`}
        >
          {modeLabel(authzMode)}
        </button>
        {showModeInfo && (
          <button
            type="button"
            onClick={() => setShowModeInfo(false)}
            title="点击收起"
            className="text-[11px] text-[#999999] hover:text-[#666666] transition-colors"
          >
            {modeDescription(authzMode)}
          </button>
        )}
      </div>
    </form>
  );
}
