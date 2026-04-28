import CollapsibleBlock from "./CollapsibleBlock";

interface Props {
  type: "call" | "result";
  name?: string;
  input?: Record<string, unknown>;
  result?: string;
  step?: number;
}

export default function ToolResultBlock({
  type,
  name,
  input,
  result,
  step,
}: Props) {
  if (type === "call") {
    return (
      <CollapsibleBlock
        title={`${name || "Tool"} ${step ? `· Step ${step}` : ""}`}
        className="border-[#d97757] bg-[#fef7f4]"
      >
        {input && (
          <div className="text-xs font-mono text-[#666666] bg-white border border-[#e5e5e5] rounded p-2 overflow-x-auto">
            <pre className="whitespace-pre-wrap break-all">
              {JSON.stringify(input, null, 2)}
            </pre>
          </div>
        )}
      </CollapsibleBlock>
    );
  }

  return (
    <CollapsibleBlock
      title={`Result ${step ? `· Step ${step}` : ""}`}
      className="border-[#22c55e] bg-[#f0fdf4]"
    >
      <div className="text-sm text-[#1a1a1a] font-mono bg-white border border-[#e5e5e5] rounded p-2 max-h-40 overflow-y-auto">
        {result}
      </div>
    </CollapsibleBlock>
  );
}
