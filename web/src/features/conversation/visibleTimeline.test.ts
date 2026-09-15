import { describe, expect, it } from "vitest";

import type { ConversationView } from "../../api/contracts";
import { visibleTimeline } from "./visibleTimeline";

const session = {
  status: "waiting",
  waiting_for: ["user_message"],
  pending_authorization_ids: [],
  should_wake: false,
  has_active_subagents: false,
  control_approval: null,
  control_message: null,
};

const conversation: ConversationView = {
  session_id: "s1",
  revision: 3,
  items: [
    {
      kind: "user",
      message_id: "user-1",
      text: "hi",
      occurred_at: "2026-09-15T08:00:00+00:00",
    },
    {
      kind: "assistant",
      message_id: "step-1",
      output_id: "user-1",
      text: "journal",
      occurred_at: "2026-09-15T08:00:01+00:00",
    },
    {
      kind: "tool",
      command_id: "cmd-1",
      name: "read_file",
      status: "running",
      occurred_at: "2026-09-15T08:00:01+00:00",
      error: null,
    },
  ],
  session,
};

describe("visibleTimeline", () => {
  it("does not repeat a final that already exists in the journal projection", () => {
    const visible = visibleTimeline(
      conversation,
      { "user-1": "journal" },
      { outputId: "user-1", text: "stream" },
      {},
    );
    expect(visible.map((item) => ("text" in item ? item.text : item.name))).toEqual([
      "hi",
      "journal",
      "read_file",
    ]);
  });

  it("shows preview only before the same output identity is committed", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };
    expect(
      visibleTimeline(empty, {}, { outputId: "user-1", text: "流" }, {}).map((item) =>
        item.kind === "tool" ? [item.name, item.status] : [item.text, item.pending],
      ),
    ).toEqual([
      ["hi", false],
      ["流", true],
    ]);
    expect(
      visibleTimeline(empty, { "user-1": "最终" }, { outputId: "user-1", text: "流" }, {}).map(
        (item) => (item.kind === "tool" ? item.name : item.text),
      ),
    ).toEqual(["hi", "最终"]);
  });

  it("merges live tool status onto journal cards by command_id", () => {
    const liveAhead = visibleTimeline(
      conversation,
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "read_file", status: "succeeded" } },
    );
    expect(liveAhead[2]).toMatchObject({
      kind: "tool",
      commandId: "cmd-1",
      status: "succeeded",
    });

    const failedTool = conversation.items[2];
    if (failedTool.kind !== "tool") {
      throw new Error("expected journal tool card");
    }
    const journalWon = visibleTimeline(
      {
        ...conversation,
        items: [
          conversation.items[0],
          conversation.items[1],
          { ...failedTool, status: "failed", error: "boom" },
        ],
      },
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "read_file", status: "running" } },
    );
    expect(journalWon[2]).toMatchObject({
      kind: "tool",
      status: "failed",
      error: "boom",
    });
  });

  it("appends a live tool that the journal projection has not caught yet", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };
    const visible = visibleTimeline(
      empty,
      {},
      { outputId: "user-1", text: "流" },
      { "cmd-2": { commandId: "cmd-2", name: "web_search", status: "running" } },
    );
    expect(visible.map((item) => item.kind)).toEqual(["user", "tool", "assistant"]);
    expect(visible[1]).toMatchObject({
      kind: "tool",
      commandId: "cmd-2",
      status: "running",
    });
  });
});
