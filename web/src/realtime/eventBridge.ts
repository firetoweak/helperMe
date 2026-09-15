import {
  connectedEventSchema,
  outputFinalEventSchema,
  previewAbortedEventSchema,
  previewDeltaEventSchema,
  previewStartedEventSchema,
  sessionActivityEventSchema,
  toolProgressEventSchema,
} from "../api/contracts";
import { helpermeApi } from "../api/helpermeApi";
import type { AppDispatch } from "../app/store";
import {
  connected,
  disconnected,
  outputFinal,
  previewAborted,
  previewDelta,
  previewStarted,
  sessionActivity,
  toolProgress,
} from "./runtimeSlice";

export function openEventBridge(dispatch: AppDispatch): () => void {
  const source = new EventSource("/api/events");

  source.addEventListener("connected", (event) => {
    const payload = connectedEventSchema.parse(JSON.parse(event.data));
    dispatch(connected(payload.connection_id));
  });
  source.addEventListener("session_activity", (event) => {
    const payload = sessionActivityEventSchema.parse(JSON.parse(event.data));
    dispatch(
      sessionActivity({
        sessionId: payload.session_id,
        activity: payload.activity,
      }),
    );
  });
  source.addEventListener("preview.started", (event) => {
    const payload = previewStartedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      previewStarted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("preview.delta", (event) => {
    const payload = previewDeltaEventSchema.parse(JSON.parse(event.data));
    dispatch(
      previewDelta({
        sessionId: payload.session_id,
        outputId: payload.output_id,
        text: payload.text,
      }),
    );
  });
  source.addEventListener("preview.aborted", (event) => {
    const payload = previewAbortedEventSchema.parse(JSON.parse(event.data));
    dispatch(
      previewAborted({
        sessionId: payload.session_id,
        outputId: payload.output_id,
      }),
    );
  });
  source.addEventListener("output_final", (event) => {
    const payload = outputFinalEventSchema.parse(JSON.parse(event.data));
    dispatch(
      outputFinal({
        sessionId: payload.session_id,
        outputId: payload.output_id,
        text: payload.text,
      }),
    );
    dispatch(
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
        "Sessions",
      ]),
    );
  });
  source.addEventListener("tool_progress", (event) => {
    const payload = toolProgressEventSchema.parse(JSON.parse(event.data));
    dispatch(
      toolProgress({
        sessionId: payload.session_id,
        commandId: payload.command_id,
        name: payload.name,
        status: payload.status,
      }),
    );
    dispatch(
      helpermeApi.util.invalidateTags([
        { type: "Conversation", id: payload.session_id },
      ]),
    );
  });
  source.addEventListener("error", () => {
    dispatch(disconnected());
  });

  return () => {
    source.close();
    dispatch(disconnected());
  };
}
