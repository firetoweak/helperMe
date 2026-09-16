import { describe, expect, it } from "vitest";

import type { ConversationView } from "../src/api/contracts";
import { timelineTurns } from "../src/features/conversation/timelineTurns";
import { visibleTimeline } from "../src/features/conversation/visibleTimeline";

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
      kind: "step",
      step_id: "step-1",
      output_id: "user-1",
      text: "journal",
      tools: [
        {
          command_id: "cmd-1",
          name: "read_file",
          status: "running",
          error: null,
        },
      ],
      occurred_at: "2026-09-15T08:00:01+00:00",
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
    expect(visible).toHaveLength(2);
    expect(visible[1]).toMatchObject({ kind: "step", text: "journal" });
  });

  it("shows preview only before the same output identity reaches the journal", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };
    expect(
      visibleTimeline(empty, {}, { outputId: "user-1", text: "流" }, {})[1],
    ).toMatchObject({
      key: "output:user-1",
      kind: "step",
      text: "流",
      pending: true,
    });
    expect(
      visibleTimeline(empty, { "user-1": "最终" }, null, {})[1],
    ).toMatchObject({
      key: "output:user-1",
      kind: "step",
      text: "最终",
      pending: true,
    });
  });

  it("mounts an empty pending step as soon as preview starts", () => {
    const empty: ConversationView = {
      ...conversation,
      revision: 1,
      items: [conversation.items[0]],
    };

    expect(
      visibleTimeline(empty, {}, { outputId: "user-1", text: "" }, {})[1],
    ).toMatchObject({
      key: "output:user-1",
      text: "",
      pending: true,
    });
  });

  it("merges live status into the matching tool inside its step", () => {
    const visible = visibleTimeline(
      conversation,
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "read_file", status: "succeeded" } },
    );
    expect(visible[1]).toMatchObject({
      kind: "step",
      tools: [{ commandId: "cmd-1", status: "succeeded" }],
    });
  });

  it("keeps journal success and failure over live progress", () => {
    const visible = visibleTimeline(
      {
        ...conversation,
        items: [
          conversation.items[0],
          {
            ...conversation.items[1],
            tools: [
              {
                command_id: "cmd-1",
                name: "read_file",
                status: "succeeded",
                error: null,
              },
            ],
          },
        ],
      },
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "read_file", status: "running" } },
    );
    expect(visible[1]).toMatchObject({
      tools: [{ commandId: "cmd-1", status: "succeeded" }],
    });
  });

  it("lets live progress update an interrupted journal card", () => {
    const visible = visibleTimeline(
      {
        ...conversation,
        items: [
          conversation.items[0],
          {
            ...conversation.items[1],
            tools: [
              {
                command_id: "cmd-1",
                name: "glob",
                status: "unknown",
                error: "执行中断，结果未知",
              },
            ],
          },
        ],
      },
      {},
      null,
      { "cmd-1": { commandId: "cmd-1", name: "glob", status: "failed" } },
    );
    expect(visible[1]).toMatchObject({
      tools: [{ commandId: "cmd-1", status: "failed", error: "执行中断，结果未知" }],
    });
  });
});

describe("timelineTurns", () => {
  it("keeps tool steps in the process and exposes the final no-tool step", () => {
    const items = visibleTimeline(
      {
        ...conversation,
        items: [
          ...conversation.items,
          {
            kind: "step",
            step_id: "step-2",
            output_id: "out-1",
            text: "final",
            tools: [],
            occurred_at: "2026-09-15T08:00:02+00:00",
          },
        ],
      },
      {},
      null,
      {},
    );

    expect(timelineTurns(items)[0]).toMatchObject({
      process: [{ key: "output:user-1" }],
      active: null,
      final: { key: "output:out-1", text: "final" },
    });
  });

  it("keeps an unfinished preview outside the execution process", () => {
    const items = visibleTimeline(
      { ...conversation, revision: 1, items: [conversation.items[0]] },
      {},
      { outputId: "user-1", text: "typing" },
      {},
    );

    expect(timelineTurns(items)[0]).toMatchObject({
      process: [],
      active: { key: "output:user-1", pending: true },
      final: null,
    });
  });
});
