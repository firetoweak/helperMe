import { z } from "zod";

const controlApprovalSchema = z
  .object({
    request_id: z.string().min(1),
    summary: z.string().min(1),
    risk: z.string().min(1),
  })
  .strict();

const sessionViewSchema = z
  .object({
    status: z.string().min(1),
    waiting_for: z.array(z.string().min(1)),
    pending_authorization_ids: z.array(z.string().min(1)),
    should_wake: z.boolean(),
    has_active_subagents: z.boolean(),
    control_approval: controlApprovalSchema.nullable(),
    control_message: z.string().nullable(),
  })
  .strict();

export const sessionSummarySchema = z
  .object({
    session_id: z.string().min(1),
    title: z.string().min(1),
    updated_at: z.string().datetime({ offset: true }).nullable(),
    activity: z.enum(["running", "idle"]),
  })
  .strict();

export const toolStatusSchema = z.enum(["running", "succeeded", "failed", "unknown"]);

const toolItemSchema = z
  .object({
    command_id: z.string().min(1),
    name: z.string().min(1),
    status: toolStatusSchema,
    error: z.string().min(1).nullable(),
  })
  .strict();

export const conversationViewSchema = z
  .object({
    session_id: z.string().min(1),
    revision: z.number().int().nonnegative(),
    items: z.array(
      z.discriminatedUnion("kind", [
        z
          .object({
            kind: z.literal("user"),
            message_id: z.string().min(1),
            text: z.string().min(1),
            occurred_at: z.string().datetime({ offset: true }),
          })
          .strict(),
        z
          .object({
            kind: z.literal("step"),
            step_id: z.string().min(1),
            output_id: z.string().min(1),
            text: z.string().min(1).nullable(),
            tools: z.array(toolItemSchema),
            occurred_at: z.string().datetime({ offset: true }),
          })
          .strict(),
      ]),
    ),
    session: sessionViewSchema,
  })
  .strict();

export const connectedEventSchema = z
  .object({ connection_id: z.string().min(1) })
  .strict();

export const sessionActivityEventSchema = z
  .object({
    session_id: z.string().min(1),
    activity: z.enum(["running", "idle"]),
  })
  .strict();

export const previewStartedEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
  })
  .strict();

export const previewDeltaEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
    text: z.string().min(1),
  })
  .strict();

export const previewAbortedEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
  })
  .strict();

export const outputFinalEventSchema = z
  .object({
    session_id: z.string().min(1),
    output_id: z.string().min(1),
    text: z.string().min(1),
  })
  .strict();

export const toolProgressEventSchema = z
  .object({
    session_id: z.string().min(1),
    command_id: z.string().min(1),
    name: z.string().min(1),
    status: toolStatusSchema,
  })
  .strict();

export type SessionSummary = z.infer<typeof sessionSummarySchema>;
export type ConversationView = z.infer<typeof conversationViewSchema>;
export type ConversationItem = ConversationView["items"][number];
export type ToolStatus = z.infer<typeof toolStatusSchema>;
