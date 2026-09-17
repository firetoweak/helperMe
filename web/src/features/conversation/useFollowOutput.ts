import { useLayoutEffect, useRef } from "react";

export function useFollowOutput(
  sessionId: string | undefined,
  active: boolean,
  attached: boolean,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const lastSessionId = useRef<string | undefined>(undefined);

  useLayoutEffect(() => {
    if (!attached) {
      return;
    }
    const viewport = viewportRef.current;
    const content = contentRef.current;
    if (viewport === null || content === null) {
      return;
    }

    const scrollToBottom = () => {
      viewport.scrollTop = viewport.scrollHeight;
    };
    const sessionChanged = lastSessionId.current !== sessionId;
    lastSessionId.current = sessionId;
    if (active || sessionChanged) {
      scrollToBottom();
    }
    if (!active) {
      return;
    }

    const observer = new ResizeObserver(scrollToBottom);
    observer.observe(content);
    return () => observer.disconnect();
  }, [active, attached, sessionId]);

  return { contentRef, viewportRef };
}
