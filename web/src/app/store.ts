import { configureStore } from "@reduxjs/toolkit";

import { helpermeApi } from "../api/helpermeApi";
import runtimeReducer, {
  hydrateDrafts,
  hydrateSuperseded,
} from "../realtime/runtimeSlice";

const DRAFT_KEY = "helperme.draftSessions";
const SUPERSEDED_KEY = "helperme.supersededSessions";

function readDrafts(): Record<string, string> {
  const raw = sessionStorage.getItem(DRAFT_KEY);
  if (raw === null || raw === "") {
    return {};
  }
  const parsed: unknown = JSON.parse(raw);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("draftSessions must be an object");
  }
  const values: Record<string, string> = {};
  for (const [workspaceId, sessionId] of Object.entries(parsed)) {
    if (typeof sessionId !== "string" || sessionId === "") {
      throw new Error("draftSessions values must be session ids");
    }
    values[workspaceId] = sessionId;
  }
  return values;
}

function readSuperseded(): Record<string, string> {
  const raw = localStorage.getItem(SUPERSEDED_KEY);
  if (raw === null || raw === "") {
    return {};
  }
  const parsed: unknown = JSON.parse(raw);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("supersededSessions must be an object");
  }
  const values: Record<string, string> = {};
  for (const [from, to] of Object.entries(parsed)) {
    if (typeof to !== "string" || to === "") {
      throw new Error("supersededSessions values must be session ids");
    }
    values[from] = to;
  }
  return values;
}

export const store = configureStore({
  reducer: {
    [helpermeApi.reducerPath]: helpermeApi.reducer,
    runtime: runtimeReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware().concat(helpermeApi.middleware),
});

const savedDrafts = sessionStorage.getItem(DRAFT_KEY);
if (savedDrafts !== null && savedDrafts !== "") {
  store.dispatch(hydrateDrafts(readDrafts()));
}

const savedSuperseded = localStorage.getItem(SUPERSEDED_KEY);
if (savedSuperseded !== null && savedSuperseded !== "") {
  store.dispatch(hydrateSuperseded(readSuperseded()));
}

store.subscribe(() => {
  const runtime = store.getState().runtime;
  if (Object.keys(runtime.draftSessions).length === 0) {
    sessionStorage.removeItem(DRAFT_KEY);
  } else {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(runtime.draftSessions));
  }
  localStorage.setItem(
    SUPERSEDED_KEY,
    JSON.stringify(runtime.supersededSessions),
  );
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
