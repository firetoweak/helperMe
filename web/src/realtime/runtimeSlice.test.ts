import { describe, expect, it } from "vitest";

import reducer, {
  outputFinal,
  previewDelta,
  previewStarted,
  sessionActivity,
  toolProgress,
  viewing,
} from "./runtimeSlice";

describe("runtimeSlice", () => {
  it("appends preview deltas and commits final text on the same output identity", () => {
    let state = reducer(undefined, previewStarted({ sessionId: "s1", outputId: "out" }));
    state = reducer(
      state,
      previewDelta({ sessionId: "s1", outputId: "out", text: "你" }),
    );
    state = reducer(
      state,
      previewDelta({ sessionId: "s1", outputId: "out", text: "好" }),
    );
    expect(state.sessions.s1.activePreview).toEqual({ outputId: "out", text: "你好" });

    state = reducer(
      state,
      outputFinal({ sessionId: "s1", outputId: "out", text: "你好" }),
    );
    expect(state.sessions.s1.activePreview).toBeNull();
    expect(state.sessions.s1.committed.out).toBe("你好");
    expect(state.sessions.s1.unread).toBe(1);
  });

  it("marks unread only when the session is not being viewed", () => {
    let state = reducer(undefined, viewing("s1"));
    state = reducer(state, sessionActivity({ sessionId: "s1", activity: "running" }));
    state = reducer(
      state,
      outputFinal({ sessionId: "s1", outputId: "out", text: "在看" }),
    );
    expect(state.sessions.s1.unread).toBe(0);

    state = reducer(state, viewing("s2"));
    state = reducer(
      state,
      outputFinal({ sessionId: "s1", outputId: "out-2", text: "离开后" }),
    );
    expect(state.sessions.s1.unread).toBe(1);
    expect(state.sessions.s2.unread).toBe(0);
  });

  it("stores tool progress on the owning session without touching the viewed session", () => {
    let state = reducer(undefined, viewing("s2"));
    state = reducer(
      state,
      toolProgress({
        sessionId: "s1",
        commandId: "cmd-1",
        name: "read_file",
        status: "running",
      }),
    );
    state = reducer(
      state,
      toolProgress({
        sessionId: "s1",
        commandId: "cmd-1",
        name: "read_file",
        status: "succeeded",
      }),
    );

    expect(state.sessions.s1.tools["cmd-1"]).toEqual({
      commandId: "cmd-1",
      name: "read_file",
      status: "succeeded",
    });
    expect(state.sessions.s2.tools).toEqual({});
    expect(state.sessions.s1.unread).toBe(0);
  });
});
