import { describe, expect, it } from "vitest";

import {
  conversationViewSchema,
  outputFinalEventSchema,
  toolProgressEventSchema,
} from "./contracts";

const session = {
  status: "waiting",
  waiting_for: ["user_message"],
  pending_authorization_ids: [],
  should_wake: false,
  has_active_subagents: false,
  control_approval: null,
  control_message: null,
};

describe("conversationViewSchema", () => {
  it("keeps step identity, output identity and nested command identity separate", () => {
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      revision: 3,
      items: [
        {
          kind: "user",
          message_id: "user-event",
          text: "hello",
          occurred_at: "2026-09-15T08:00:00+00:00",
        },
        {
          kind: "step",
          step_id: "step-event",
          output_id: "user-event",
          text: "world",
          tools: [
            {
              command_id: "cmd-1",
              name: "read_file",
              status: "succeeded",
              error: null,
            },
          ],
          occurred_at: "2026-09-15T08:00:01+00:00",
        },
      ],
      session,
    });

    expect(parsed.items[1]).toMatchObject({
      kind: "step",
      step_id: "step-event",
      output_id: "user-event",
    });
    expect(parsed.items[1]).toMatchObject({
      kind: "step",
      tools: [{
      command_id: "cmd-1",
      status: "succeeded",
      }],
    });
  });

  it("rejects a user item that carries an output identity", () => {
    expect(() =>
      conversationViewSchema.parse({
        session_id: "session-1",
        revision: 1,
        items: [
          {
            kind: "user",
            message_id: "user-event",
            output_id: "wrong",
            text: "hello",
            occurred_at: "2026-09-15T08:00:00+00:00",
          },
        ],
        session,
      }),
    ).toThrow();
  });
});

describe("outputFinalEventSchema", () => {
  it("requires a session and the same output identity used by preview", () => {
    const parsed = outputFinalEventSchema.parse({
      session_id: "session-1",
      output_id: "user-event",
      text: "world",
    });
    expect(parsed.output_id).toBe("user-event");
  });
});

describe("toolProgressEventSchema", () => {
  it("identifies a tool card by command_id and a terminal-or-running status", () => {
    const parsed = toolProgressEventSchema.parse({
      session_id: "session-1",
      command_id: "cmd-1",
      name: "read_file",
      status: "running",
    });
    expect(parsed.command_id).toBe("cmd-1");
  });

  it("accepts unknown as a journal tool status", () => {
    const parsed = conversationViewSchema.parse({
      session_id: "session-1",
      revision: 1,
      items: [
        {
          kind: "step",
          step_id: "step-event",
          output_id: "user-event",
          text: null,
          tools: [
            {
              command_id: "cmd-1",
              name: "glob",
              status: "unknown",
              error: "执行中断，结果未知",
            },
          ],
          occurred_at: "2026-09-15T08:00:01+00:00",
        },
      ],
      session,
    });
    expect(parsed.items[0]).toMatchObject({
      tools: [{ command_id: "cmd-1", status: "unknown" }],
    });
  });
});
