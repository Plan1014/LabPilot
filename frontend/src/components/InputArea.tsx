import React, { useState } from "react";
import { PaperPlaneTilt, StopCircle } from "@phosphor-icons/react";

interface Props {
  onSubmit: (query: string) => void;
  isStreaming: boolean;
}

export default function InputArea({ onSubmit, isStreaming }: Props) {
  const [input, setInput] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (input.trim() && !isStreaming) {
      onSubmit(input.trim());
      setInput("");
    }
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
          placeholder="Ask LabPilot..."
          disabled={isStreaming}
          className="flex-1 px-4 py-2 border border-[#d1d5db] rounded-lg text-sm
                     focus:outline-none focus:border-[#d97757] focus:ring-1 focus:ring-[#d97757]
                     disabled:bg-[#f5f5f5] disabled:text-[#999999]
                     placeholder:text-[#9ca3af]"
        />
        <button
          type="submit"
          disabled={!input.trim() || isStreaming}
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
    </form>
  );
}
