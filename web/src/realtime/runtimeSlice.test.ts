import { describe, expect, it } from "vitest";

import reducer, {
  bindOwner,
  connected,
  contextUsage,
  disconnected,
  lockDraft,
  outputFinal,
  previewDelta,
  previewStarted,
  sessionActivity,
  setDraftSession,
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

  it("stores context usage on the owning session", () => {
    let state = reducer(undefined, viewing("s2"));
    state = reducer(
      state,
      contextUsage({ sessionId: "s1", used: 1200, limit: 200000 }),
    );
    expect(state.sessions.s1.contextUsage).toEqual({ used: 1200, limit: 200000 });
    expect(state.sessions.s2.contextUsage).toBeNull();
  });

  it("keeps a single unlocked draft until the first user message locks it", () => {
    let state = reducer(undefined, setDraftSession("draft-1"));
    expect(state.draftSessionId).toBe("draft-1");
    state = reducer(state, lockDraft("other"));
    expect(state.draftSessionId).toBe("draft-1");
    state = reducer(state, lockDraft("draft-1"));
    expect(state.draftSessionId).toBeNull();
  });

  it("clears the host owner on disconnect without dropping the unlocked draft", () => {
    let state = reducer(undefined, connected("conn-1"));
    state = reducer(state, setDraftSession("draft-1"));
    state = reducer(state, bindOwner("draft-1"));
    state = reducer(state, disconnected());
    expect(state.connectionId).toBeNull();
    expect(state.ownerSessionId).toBeNull();
    expect(state.draftSessionId).toBe("draft-1");
  });
});
