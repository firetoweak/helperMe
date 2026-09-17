import { describe, expect, it } from "vitest";

import { stepHeading } from "../src/features/conversation/stepHeading";
import type { VisibleTool } from "../src/features/conversation/visibleTimeline";

function tool(
  name: string,
  arguments_: Record<string, unknown> = {},
): VisibleTool {
  return {
    commandId: name,
    name,
    status: "succeeded",
    error: null,
    arguments: arguments_,
  };
}

describe("stepHeading", () => {
  it("uses tool names and a short argument instead of a generic step number", () => {
    expect(
      stepHeading({
        text: "ignored when tools exist",
        tools: [tool("read_file", { path: "a.py" }), tool("grep", { pattern: "foo" })],
      }),
    ).toBe("read_file  a.py · grep  foo");
  });

  it("falls back to clipped step text when a process step has no tools", () => {
    expect(
      stepHeading({
        text: `${"x".repeat(50)}`,
        tools: [],
      }),
    ).toBe(`${"x".repeat(47)}…`);
  });
});
