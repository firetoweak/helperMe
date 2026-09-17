import { describe, expect, it } from "vitest";

import { createTextDeltaBuffer, type TextDelta } from "./textDeltaBuffer";

describe("createTextDeltaBuffer", () => {
  it("concatenates same-identity deltas until the scheduled flush", () => {
    const published: TextDelta[] = [];
    const scheduled: { flush: (() => void) | null } = { flush: null };
    const buffer = createTextDeltaBuffer(
      (delta) => published.push(delta),
      (flush) => {
        scheduled.flush = flush;
        return () => {
          scheduled.flush = null;
        };
      },
    );

    buffer.enqueue({ sessionId: "s1", outputId: "out", text: "想" });
    buffer.enqueue({ sessionId: "s1", outputId: "out", text: "一" });
    expect(published).toEqual([]);

    scheduled.flush?.();
    expect(published).toEqual([
      { sessionId: "s1", outputId: "out", text: "想一" },
    ]);
  });

  it("publishes the previous identity immediately when the stream changes", () => {
    const published: TextDelta[] = [];
    const buffer = createTextDeltaBuffer(
      (delta) => published.push(delta),
      () => () => {},
    );

    buffer.enqueue({ sessionId: "s1", outputId: "out-1", text: "甲" });
    buffer.enqueue({ sessionId: "s1", outputId: "out-2", text: "乙" });
    expect(published).toEqual([
      { sessionId: "s1", outputId: "out-1", text: "甲" },
    ]);

    buffer.flushNow();
    expect(published).toEqual([
      { sessionId: "s1", outputId: "out-1", text: "甲" },
      { sessionId: "s1", outputId: "out-2", text: "乙" },
    ]);
  });
});
