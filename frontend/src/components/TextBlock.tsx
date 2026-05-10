import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface Props {
  content: string | string[];
}

export default function TextBlock({ content }: Props) {
  const texts = Array.isArray(content) ? content : [content];

  return (
    <div className="text-sm text-[#1a1a1a] leading-relaxed">
      {texts.map((text, i) => (
        <div
          key={i}
          className="prose prose-sm max-w-none prose-p:my-1 prose-pre:bg-[#f5f5f5] prose-code:text-[#d97757] prose-code:bg-[#fef7f4] prose-code:px-1 prose-code:rounded"
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </div>
      ))}
    </div>
  );
}
