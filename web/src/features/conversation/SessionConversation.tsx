import { useParams } from "react-router-dom";

import { Conversation } from "./Conversation";

export function SessionConversation() {
  const { sessionId } = useParams();
  return <Conversation key={sessionId} />;
}
