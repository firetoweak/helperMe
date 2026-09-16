import { configureStore } from "@reduxjs/toolkit";

import { helpermeApi } from "../api/helpermeApi";
import runtimeReducer, { setDraftSession } from "../realtime/runtimeSlice";

const DRAFT_KEY = "helperme.draftSessionId";

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

store.subscribe(() => {
  const draftSessionId = store.getState().runtime.draftSessionId;
  if (draftSessionId === null) {
    sessionStorage.removeItem(DRAFT_KEY);
  } else {
    sessionStorage.setItem(DRAFT_KEY, draftSessionId);
  }
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
