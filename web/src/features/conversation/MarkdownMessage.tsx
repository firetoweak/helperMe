import {
  createRemendPreprocessor,
  useSmoothStream,
} from "@ai-markdown/react";
import MantineAIMarkdown from "@ai-markdown/react-mantine";
import hljs from "highlight.js/lib/common";

const codeBlock = {
  autoDetectUnknownLanguage: true,
  highlightJs: hljs,
} as const;
const contentPreprocessors = [createRemendPreprocessor()];

interface MarkdownMessageProps {
  content: string;
  streaming: boolean;
}

export function MarkdownMessage({ content, streaming }: MarkdownMessageProps) {
  const smooth = useSmoothStream({ content, streaming, pacing: "smooth" });

  return (
    <div
      className="markdown-message"
      data-streaming={smooth.streaming || undefined}
    >
      <MantineAIMarkdown
        codeBlock={codeBlock}
        content={smooth.content}
        contentPreprocessors={contentPreprocessors}
        fontSize={14}
        streaming={smooth.streaming}
      />
    </div>
  );
}
