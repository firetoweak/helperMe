import { configureStore } from "@reduxjs/toolkit";

import { helpermeApi } from "../api/helpermeApi";
import runtimeReducer from "../realtime/runtimeSlice";

export const store = configureStore({
  reducer: {
    [helpermeApi.reducerPath]: helpermeApi.reducer,
    runtime: runtimeReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware().concat(helpermeApi.middleware),
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
