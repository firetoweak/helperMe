import { configureStore } from "@reduxjs/toolkit";

import { helpermeApi } from "../api/helpermeApi";
import runtimeReducer, {
  hydrateSuperseded,
  setDraftSession,
} from "../realtime/runtimeSlice";

const DRAFT_KEY = "helperme.draftSessionId";
const SUPERSEDED_KEY = "helperme.supersededSessions";

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

const savedDraft = sessionStorage.getItem(DRAFT_KEY);
if (savedDraft !== null && savedDraft !== "") {
  store.dispatch(setDraftSession(savedDraft));
}

const savedSuperseded = localStorage.getItem(SUPERSEDED_KEY);
if (savedSuperseded !== null && savedSuperseded !== "") {
  store.dispatch(hydrateSuperseded(readSuperseded()));
}

store.subscribe(() => {
  const runtime = store.getState().runtime;
  if (runtime.draftSessionId === null) {
    sessionStorage.removeItem(DRAFT_KEY);
  } else {
    sessionStorage.setItem(DRAFT_KEY, runtime.draftSessionId);
  }
  localStorage.setItem(
    SUPERSEDED_KEY,
    JSON.stringify(runtime.supersededSessions),
  );
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
