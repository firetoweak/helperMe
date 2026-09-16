import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import { z } from "zod";

import { bindOwner } from "../realtime/runtimeSlice";
import {
  conversationViewSchema,
  runtimeStatusSchema,
  sessionSummarySchema,
  type ConversationView,
  type RuntimeStatus,
  type SessionSummary,
} from "./contracts";

type SelectSession = {
  connectionId: string;
  sessionId: string;
};

type SendInput = SelectSession & {
  deliveryId: string;
  text: string;
  artifactRefs: string[];
};

type EditAndFork = SelectSession & {
  deliveryId: string;
  text: string;
  messageId: string;
};

type UploadAttachment = SelectSession & {
  file: File;
};

const attachmentRefSchema = z
  .object({
    attachment_id: z.string().regex(/^sha256:[0-9a-f]{64}$/),
    mime: z.enum(["image/png", "image/jpeg", "image/webp", "image/gif"]),
    width: z.number().int().positive(),
    height: z.number().int().positive(),
  })
  .strict();

export type AttachmentRef = z.infer<typeof attachmentRefSchema>;

function putConversation(
  dispatch: (action: unknown) => void,
  getState: () => unknown,
  sessionId: string,
  data: ConversationView,
) {
  const current = helpermeApi.endpoints.getConversation.select(sessionId)(
    getState() as never,
  ).data;
  if (current !== undefined && current.revision > data.revision) {
    return;
  }
  dispatch(
    helpermeApi.util.upsertQueryData("getConversation", sessionId, data),
  );
}

export const helpermeApi = createApi({
  reducerPath: "helpermeApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Sessions", "Conversation"],
  endpoints: (build) => ({
    getRuntime: build.query<RuntimeStatus, void>({
      query: () => "/runtime",
      transformResponse: (value: unknown) => runtimeStatusSchema.parse(value),
    }),
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
      async onQueryStarted(_connectionId, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(data.session_id));
        putConversation(dispatch, getState, data.session_id, data);
      },
    }),
    selectSession: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/select`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        dispatch(bindOwner(arg.sessionId));
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
    sendInput: build.mutation<ConversationView, SendInput>({
      query: ({ connectionId, sessionId, deliveryId, text, artifactRefs }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/inputs`,
        method: "POST",
        body: {
          connection_id: connectionId,
          delivery_id: deliveryId,
          text,
          artifact_refs: artifactRefs,
        },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      invalidatesTags: ["Sessions"],
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
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
      async onQueryStarted(_arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, data.session_id, data);
      },
    }),
    uploadAttachment: build.mutation<AttachmentRef, UploadAttachment>({
      query: ({ connectionId, sessionId, file }) => {
        const body = new FormData();
        body.append("connection_id", connectionId);
        body.append("file", file);
        return {
          url: `/sessions/${encodeURIComponent(sessionId)}/attachments`,
          method: "POST",
          body,
        };
      },
      transformResponse: (value: unknown) => attachmentRefSchema.parse(value),
    }),
    cancelTurn: build.mutation<ConversationView, SelectSession>({
      query: ({ connectionId, sessionId }) => ({
        url: `/sessions/${encodeURIComponent(sessionId)}/cancel`,
        method: "POST",
        body: { connection_id: connectionId },
      }),
      transformResponse: (value: unknown) => conversationViewSchema.parse(value),
      async onQueryStarted(arg, { dispatch, getState, queryFulfilled }) {
        const { data } = await queryFulfilled;
        putConversation(dispatch, getState, arg.sessionId, data);
      },
    }),
  }),
});

export const {
  useCreateSessionMutation,
  useGetRuntimeQuery,
  useGetSessionsQuery,
  useGetConversationQuery,
  useSelectSessionMutation,
  useSendInputMutation,
  useEditAndForkMutation,
  useUploadAttachmentMutation,
  useCancelTurnMutation,
} = helpermeApi;
