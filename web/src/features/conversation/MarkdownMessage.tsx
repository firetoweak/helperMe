import { createRemendPreprocessor } from "@ai-markdown/react";
import MantineAIMarkdown from "@ai-markdown/react-mantine";
import hljs from "highlight.js/lib/common";
import { memo } from "react";

const highlightedCodeBlock = {
  autoDetectUnknownLanguage: true,
  highlightJs: hljs,
} as const;
const contentPreprocessors = [createRemendPreprocessor()];

interface MarkdownMessageProps {
  content: string;
  streaming: boolean;
}

export const MarkdownMessage = memo(function MarkdownMessage({
  content,
  streaming,
}: MarkdownMessageProps) {
  return (
    <div className="markdown-message" data-streaming={streaming || undefined}>
      <MantineAIMarkdown
        codeBlock={streaming ? undefined : highlightedCodeBlock}
        content={content}
        contentPreprocessors={contentPreprocessors}
        fontSize={14}
        streaming={streaming}
      />
    </div>
  );
});
