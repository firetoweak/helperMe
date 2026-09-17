import { describe, expect, it } from "vitest";

import {
  isSubagentTool,
  subagentChildSessionId,
  subagentTask,
  turnNeedsSubagentHint,
} from "../src/features/conversation/subagent";
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

describe("subagent call identity", () => {
  it("treats only delegate and reclaim as SubAgent tools", () => {
    expect(isSubagentTool("delegate")).toBe(true);
    expect(isSubagentTool("reclaim")).toBe(true);
    expect(isSubagentTool("execute_command")).toBe(false);
  });

  it("reads the task or child session id from the special tool arguments", () => {
    expect(subagentTask(tool("delegate", { task: "查 pi 的展示层" }))).toBe(
      "查 pi 的展示层",
    );
    expect(
      subagentChildSessionId(
        tool("reclaim", { child_session_id: "session-1/sub-command_a" }),
      ),
    ).toBe("session-1/sub-command_a");
  });
});

describe("subagent activity hint", () => {
  it("shows only on the latest turn while children are still pending", () => {
    expect(turnNeedsSubagentHint(true, true)).toBe(true);
    expect(turnNeedsSubagentHint(false, true)).toBe(false);
    expect(turnNeedsSubagentHint(true, false)).toBe(false);
  });
});
