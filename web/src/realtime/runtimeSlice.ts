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

export type PendingAuthorization = {
  sessionId: string;
  commandId: string;
  name: string;
  arguments: Record<string, unknown>;
};

export type SessionRuntime = {
  activity: SessionActivity | null;
  lastError: string | null;
  activePreview: ActivePreview | null;
  activeThinking: ActivePreview | null;
  unread: number;
  committed: Record<string, string>;
  committedThinking: Record<string, string>;
  tools: Record<string, LiveTool>;
  authorizations: Record<string, PendingAuthorization>;
  contextUsage: { used: number; limit: number } | null;
  controlNotice: string | null;
};

type RuntimeState = {
  connectionId: string | null;
  viewingSessionId: string | null;
  ownerSessionId: string | null;
  draftSessions: Record<string, string>;
  supersededSessions: Record<string, string>;
  sessions: Record<string, SessionRuntime>;
};

const initialState: RuntimeState = {
  connectionId: null,
  viewingSessionId: null,
  ownerSessionId: null,
  draftSessions: {},
  supersededSessions: {},
  sessions: {},
};

function runtimeOf(state: RuntimeState, sessionId: string): SessionRuntime {
  const current = state.sessions[sessionId];
  if (current !== undefined) {
    return current;
  }
  const created: SessionRuntime = {
    activity: null,
    lastError: null,
    activePreview: null,
    activeThinking: null,
    unread: 0,
    committed: {},
    committedThinking: {},
    tools: {},
    authorizations: {},
    contextUsage: null,
    controlNotice: null,
  };
  state.sessions[sessionId] = created;
  return created;
}

export function liveSessionId(
  sessionId: string,
  superseded: Record<string, string>,
): string {
  let current = sessionId;
  const seen = new Set<string>();
  while (superseded[current] !== undefined && !seen.has(current)) {
    seen.add(current);
    current = superseded[current];
  }
  return current;
}

export function isForkIdentity(
  sessionId: string,
  superseded: Record<string, string>,
): boolean {
  return Object.values(superseded).includes(sessionId);
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
      state.ownerSessionId = null;
    },
    viewing(state, action: PayloadAction<string | null>) {
      state.viewingSessionId = action.payload;
      if (action.payload !== null) {
        runtimeOf(state, action.payload).unread = 0;
      }
    },
    hydrateDrafts(
      state,
      action: PayloadAction<Record<string, string>>,
    ) {
      state.draftSessions = action.payload;
    },
    setDraftSession(
      state,
      action: PayloadAction<{ workspaceId: string; sessionId: string }>,
    ) {
      state.draftSessions[action.payload.workspaceId] = action.payload.sessionId;
    },
    lockDraft(state, action: PayloadAction<string>) {
      for (const [workspaceId, sessionId] of Object.entries(state.draftSessions)) {
        if (sessionId === action.payload) {
          delete state.draftSessions[workspaceId];
        }
      }
    },
    bindOwner(state, action: PayloadAction<string>) {
      state.ownerSessionId = action.payload;
    },
    hydrateSuperseded(state, action: PayloadAction<Record<string, string>>) {
      state.supersededSessions = action.payload;
    },
    supersedeSession(
      state,
      action: PayloadAction<{ from: string; to: string }>,
    ) {
      state.supersededSessions[action.payload.from] = action.payload.to;
    },
    clearLiveOutput(state, action: PayloadAction<string>) {
      const session = runtimeOf(state, action.payload);
      session.lastError = null;
      session.activePreview = null;
      session.activeThinking = null;
      session.committed = {};
      session.committedThinking = {};
      session.tools = {};
      session.authorizations = {};
      session.contextUsage = null;
      session.controlNotice = null;
    },
    controlNotice(
      state,
      action: PayloadAction<{ sessionId: string; message: string | null }>,
    ) {
      runtimeOf(state, action.payload.sessionId).controlNotice =
        action.payload.message;
    },
    sessionActivity(
      state,
      action: PayloadAction<{ sessionId: string; activity: SessionActivity }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      session.activity = action.payload.activity;
      if (action.payload.activity === "running") {
        session.lastError = null;
      }
    },
    sessionFailed(
      state,
      action: PayloadAction<{ sessionId: string; message: string }>,
    ) {
      runtimeOf(state, action.payload.sessionId).lastError = action.payload.message;
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
    thinkingStarted(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      runtimeOf(state, action.payload.sessionId).activeThinking = {
        outputId: action.payload.outputId,
        text: "",
      };
    },
    thinkingDelta(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string; text: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (
        session.activeThinking === null ||
        session.activeThinking.outputId !== action.payload.outputId
      ) {
        return;
      }
      session.activeThinking.text += action.payload.text;
    },
    thinkingClosed(
      state,
      action: PayloadAction<{ sessionId: string; outputId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      if (session.activeThinking?.outputId !== action.payload.outputId) {
        return;
      }
      if (session.activeThinking.text.trim() !== "") {
        session.committedThinking[action.payload.outputId] =
          session.activeThinking.text;
      }
      session.activeThinking = null;
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
    authorizationRequired(
      state,
      action: PayloadAction<PendingAuthorization>,
    ) {
      const { sessionId, commandId, name, arguments: args } = action.payload;
      const session = runtimeOf(state, sessionId);
      session.authorizations[commandId] = {
        sessionId,
        commandId,
        name,
        arguments: args,
      };
      if (state.viewingSessionId !== sessionId) {
        session.unread += 1;
      }
    },
    authorizationResolved(
      state,
      action: PayloadAction<{ sessionId: string; commandId: string }>,
    ) {
      const session = runtimeOf(state, action.payload.sessionId);
      delete session.authorizations[action.payload.commandId];
    },
    contextUsage(
      state,
      action: PayloadAction<{ sessionId: string; used: number; limit: number }>,
    ) {
      runtimeOf(state, action.payload.sessionId).contextUsage = {
        used: action.payload.used,
        limit: action.payload.limit,
      };
    },
  },
});

export const {
  connected,
  disconnected,
  viewing,
  hydrateDrafts,
  setDraftSession,
  lockDraft,
  bindOwner,
  hydrateSuperseded,
  supersedeSession,
  clearLiveOutput,
  controlNotice,
  sessionActivity,
  sessionFailed,
  previewStarted,
  previewDelta,
  previewAborted,
  thinkingStarted,
  thinkingDelta,
  thinkingClosed,
  outputFinal,
  toolProgress,
  authorizationRequired,
  authorizationResolved,
  contextUsage,
} = runtimeSlice.actions;
export default runtimeSlice.reducer;
