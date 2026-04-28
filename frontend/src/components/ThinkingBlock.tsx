import CollapsibleBlock from "./CollapsibleBlock";

interface Props {
  blocks: string[];
}

export default function ThinkingBlock({ blocks }: Props) {
  const thinkingText = blocks.join("\n\n");

  return (
    <CollapsibleBlock title="Thinking" className="border-l-4 border-l-[#d1d5db]">
      <div className="text-xs text-[#9ca3af] font-mono whitespace-pre-wrap">
        {thinkingText}
      </div>
    </CollapsibleBlock>
  );
}
