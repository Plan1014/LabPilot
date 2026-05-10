import { useState, ReactNode } from "react";
import { CaretDown, CaretRight } from "@phosphor-icons/react";

interface Props {
  title: string;
  children: ReactNode;
  defaultCollapsed?: boolean;
  className?: string;
}

export default function CollapsibleBlock({
  title,
  children,
  defaultCollapsed = false,
  className = "",
}: Props) {
  const [isCollapsed, setIsCollapsed] = useState(defaultCollapsed);

  return (
    <div className={`border border-[#e5e5e5] rounded-lg overflow-hidden ${className}`}>
      <button
        type="button"
        onClick={() => setIsCollapsed(!isCollapsed)}
        className="w-full flex items-center gap-2 px-3 py-2 text-sm text-[#666666] hover:bg-[#f0f0f0] transition-colors"
      >
        {isCollapsed ? (
          <CaretRight size={14} weight="regular" />
        ) : (
          <CaretDown size={14} weight="regular" />
        )}
        <span>{title}</span>
      </button>
      {!isCollapsed && <div className="px-3 py-2">{children}</div>}
    </div>
  );
}
