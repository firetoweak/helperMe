import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

import type { ToolStatus } from "../api/contracts";

export type SessionActivity = "running" | "idle";

export type ActivePreview = {
  outputId: string;
  text: string;
};

export type LiveTool = {
  commandId: string;
  name: string;
  status: ToolStatus;
};

export type SessionRuntime = {
  activity: SessionActivity;
  activePreview: ActivePreview | null;
  unread: number;
  committed: Record<string, string>;
  tools: Record<string, LiveTool>;
};

type RuntimeState = {
  connectionId: string | null;
  viewingSessionId: string | null;
  sessions: Record<string, SessionRuntime>;
};

const initialState: RuntimeState = {
  connectionId: null,
  viewingSessionId: null,
  sessions: {},
};

function runtimeOf(state: RuntimeState, sessionId: string): SessionRuntime {
  const current = state.sessions[sessionId];
  if (current !== undefined) {
    return current;
  }
  const created: SessionRuntime = {
    activity: "idle",
    activePreview: null,
    unread: 0,
    committed: {},
    tools: {},
  };
  state.sessions[sessionId] = created;
  return created;
}

const runtimeSlice = createSlice({
  name: "runtime",
  initialState,
  reducers: {
    connected(state, action: PayloadAction<string>) {
      state.connectionId = action.payload;
    },
    disconnected(state) {
      state.connectionId = null;
    },
    viewing(state, action: PayloadAction<string | null>) {
      state.viewingSessionId = action.payload;
      if (action.payload !== null) {
        runtimeOf(state, action.payload).unread = 0;
      }
    },
    sessionActivity(
      state,
      action: PayloadAction<{ sessionId: string; activity: SessionActivity }>,
    ) {
      runtimeOf(state, action.payload.sessionId).activity = action.payload.activity;
    },
    previewStarted(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      runtimeOf(state, action.payload.sessionId).activePreview = {
        outputId: action.payload.outputId,
        text: "",
      };
    },
    previewDelta(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string; text: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (
        session.activePreview === null ||
        session.activePreview.outputId !== action.payload.outputId
      ) {
        return;
      }
      session.activePreview.text += action.payload.text;
    },
    previewAborted(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (session.activePreview?.outputId === action.payload.outputId) {
        session.activePreview = null;
      }
    },
    outputFinal(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string; text: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.committed[action.payload.outputId] = action.payload.text;
      if (session.activePreview?.outputId === action.payload.outputId) {
        session.activePreview = null;
      }
      if (state.viewingSessionId !== action.payload.sessionId) {
        session.unread += 1;
      }
    },
    toolProgress(
      state,
      action: PayloadAction<{
        sessionId: string;
        commandId: string;
        name: string;
        status: ToolStatus;
      }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.tools[action.payload.commandId] = {
        commandId: action.payload.commandId,
        name: action.payload.name,
        status: action.payload.status,
      };
    },
  },
});

export const {
  connected,
  disconnected,
  viewing,
  sessionActivity,
  previewStarted,
  previewDelta,
  previewAborted,
  outputFinal,
  toolProgress,
} = runtimeSlice.actions;
export default runtimeSlice.reducer;
