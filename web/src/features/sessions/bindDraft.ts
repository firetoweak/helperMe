import type { ConversationView } from "../../api/contracts";
import type { AppDispatch } from "../../app/store";
import { setDraftSession } from "../../realtime/runtimeSlice";

type CreateSession = (connectionId: string) => {
  unwrap: () => Promise<ConversationView>;
};

let inflight: Promise<string> | null = null;

export function bindDraftSession(
  connectionId: string,
  createSession: CreateSession,
  dispatch: AppDispatch,
): Promise<string> {
  if (inflight !== null) {
    return inflight;
  }
  inflight = (async () => {
    try {
      const conversation = await createSession(connectionId).unwrap();
      dispatch(setDraftSession(conversation.session_id));
      return conversation.session_id;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}
