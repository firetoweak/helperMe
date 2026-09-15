import { useEffect } from "react";
import { useParams } from "react-router-dom";

import {
  useCancelTurnMutation,
  useGetConversationQuery,
  useSelectSessionMutation,
  useSendInputMutation,
} from "../../api/helpermeApi";
import type { ToolStatus } from "../../api/contracts";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { viewing } from "../../realtime/runtimeSlice";
import { Composer } from "./Composer";
import { visibleTimeline } from "./visibleTimeline";

const TOOL_STATUS_LABEL: Record<ToolStatus, string> = {
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
};

export function Conversation() {
  const dispatch = useAppDispatch();
  const { sessionId } = useParams();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const runtime = useAppSelector((state) =>
    sessionId === undefined ? undefined : state.runtime.sessions[sessionId],
  );
  const selected = useGetConversationQuery(sessionId ?? "", {
    skip: sessionId === undefined,
  });
  const [selectSession] = useSelectSessionMutation();
  const [sendInput, sending] = useSendInputMutation();
  const [cancelTurn, cancelling] = useCancelTurnMutation();

  useEffect(() => {
    dispatch(viewing(sessionId ?? null));
    return () => {
      dispatch(viewing(null));
    };
  }, [dispatch, sessionId]);

  useEffect(() => {
    if (connectionId === null || sessionId === undefined) {
      return;
    }
    void selectSession({ connectionId, sessionId });
  }, [connectionId, sessionId, selectSession]);

  if (selected.isLoading || selected.isUninitialized) {
    return <div className="pane-message">正在打开 Session…</div>;
  }
  if (selected.isError || selected.data === undefined || sessionId === undefined) {
    const missing =
      selected.isError &&
      "status" in selected.error &&
      selected.error.status === 404;
    return (
      <div className="pane-message pane-error">
        {missing ? "这个 Session 不存在。请从左侧新建或选择会话。" : "Session 加载失败"}
      </div>
    );
  }

  const conversation = selected.data;
  const currentSessionId = sessionId;
  const items = visibleTimeline(
    conversation,
    runtime?.committed ?? {},
    runtime?.activePreview ?? null,
    runtime?.tools ?? {},
  );
  const running = runtime?.activity === "running";

  async function send(text: string) {
    if (connectionId === null) {
      return;
    }
    await sendInput({
      connectionId,
      sessionId: currentSessionId,
      deliveryId: `web-${crypto.randomUUID()}`,
      text,
    }).unwrap();
  }

  return (
    <section className={items.length === 0 ? "conversation conversation-empty" : "conversation"}>
      {items.length === 0 ? (
        <div className="conversation-intro">
          <h1>开始新的会话</h1>
          <p>{shortId(conversation.session_id)}</p>
        </div>
      ) : (
        <div className="timeline">
          {items.map((item) =>
            item.kind === "tool" ? (
              <article
                className={`tool-card tool-card-${item.status}`}
                key={item.key}
              >
                <span className="tool-name">{item.name}</span>
                <span className={`tool-status tool-status-${item.status}`}>
                  {TOOL_STATUS_LABEL[item.status]}
                </span>
                {item.error === null ? null : <p className="tool-error">{item.error}</p>}
              </article>
            ) : (
              <article
                className={
                  item.pending
                    ? `message message-${item.kind} message-pending`
                    : `message message-${item.kind}`
                }
                key={item.key}
              >
                {item.text}
              </article>
            ),
          )}
        </div>
      )}
      <Composer
        disabled={connectionId === null}
        sending={sending.isLoading}
        running={running}
        cancelling={cancelling.isLoading}
        onSend={send}
        onCancel={() => {
          if (connectionId === null) {
            return;
          }
          void cancelTurn({ connectionId, sessionId: currentSessionId }).unwrap();
        }}
      />
    </section>
  );
}

function shortId(id: string) {
  return `Session · ${id.slice(-8)}`;
}
