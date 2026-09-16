import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import {
  conversationViewSchema,
  sessionSummarySchema,
  type ConversationView,
  type SessionSummary,
} from "./contracts";

type SelectSession = {
  connectionId: string;
  sessionId: string;
};

type SendInput = SelectSession & {
  deliveryId: string;
  text: string;
};

type EditAndFork = SendInput & {
  messageId: string;
};

export const helpermeApi = createApi({
  reducerPath: "helpermeApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Sessions", "Conversation"],
  endpoints: (build) => ({
    getSessions: build.query<SessionSummary[], void>({
      query: () => "/sessions",
      transformResponse: (value: unknown) =>
        sessionSummarySchema.array().parse(value),
      providesTags: ["Sessions"],
    }),
    getConversation: build.query<ConversationView, string>({
      query: (sessionId) => `/sessions/${encodeURIComponent(sessionId)}`,
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      providesTags: (_result, _error, sessionId) => [
        { type: "Conversation", id: sessionId },
      ],
    }),
    createSession: build.mutation<ConversationView, string>({
      query: (connectionId) => ({
        url: "/sessions",
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(_connectionId, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(
          helpermeApi.util.upsertQueryData("getConversation", data.session_id, data),
        );
      },
    }),
    selectSession: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/select`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(
          helpermeApi.util.upsertQueryData("getConversation", arg.sessionId, data),
        );
      },
    }),
    sendInput: build.mutation<ConversationView, SendInput>({
      query: ({ connectionId, sessionId, deliveryId, text }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/inputs`,
        method: "POST",
        body: {
          connection_id: connectionId,
          delivery_id: deliveryId,
          text,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(
          helpermeApi.util.upsertQueryData("getConversation", arg.sessionId, data),
        );
      },
    }),
    editAndFork: build.mutation<ConversationView, EditAndFork>({
      query: ({ connectionId, sessionId, messageId, deliveryId, text }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/forks`,
        method: "POST",
        body: {
          connection_id: connectionId,
          message_id: messageId,
          delivery_id: deliveryId,
          text,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(
          helpermeApi.util.upsertQueryData(
            "getConversation",
            data.session_id,
            data,
          ),
        );
      },
    }),
    cancelTurn: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/cancel`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(
          helpermeApi.util.upsertQueryData("getConversation", arg.sessionId, data),
        );
      },
    }),
  }),
});

export const {
  useCreateSessionMutation,
  useGetSessionsQuery,
  useGetConversationQuery,
  useSelectSessionMutation,
  useSendInputMutation,
  useEditAndForkMutation,
  useCancelTurnMutation,
} = helpermeApi;
