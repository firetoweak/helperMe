import { useNavigate, useParams } from "react-router-dom";

import {
  useCreateSessionMutation,
  useGetSessionsQuery,
} from "../../api/helpermeApi";
import { useAppSelector } from "../../app/hooks";

export function SessionSidebar() {
  const navigate = useNavigate();
  const { sessionId } = useParams();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const runtimes = useAppSelector((state) => state.runtime.sessions);
  const { data: sessions = [], isLoading } = useGetSessionsQuery();
  const [createSession, creation] = useCreateSessionMutation();

  async function create() {
    if (connectionId === null) return;
    const conversation = await createSession(connectionId).unwrap();
    navigate(`/sessions/${encodeURIComponent(conversation.session_id)}`);
  }

  return (
    <aside className="sidebar">
      <div className="brand">HelperMe</div>
      <button
        className="new-session"
        type="button"
        disabled={connectionId === null || creation.isLoading}
        onClick={create}
      >
        <span aria-hidden="true">＋</span>
        新建会话
      </button>
      <div className="section-label">Sessions</div>
      <nav className="session-list" aria-label="Sessions">
        {isLoading ? <div className="sidebar-note">正在读取…</div> : null}
        {sessions.map((session) => {
          const runtime = runtimes[session.session_id];
          const activity = runtime?.activity ?? session.activity;
          const unread = runtime?.unread ?? 0;
          return (
            <button
              className={
                session.session_id === sessionId
                  ? "session-item session-item-active"
                  : "session-item"
              }
              key={session.session_id}
              type="button"
              onClick={() =>
                navigate(`/sessions/${encodeURIComponent(session.session_id)}`)
              }
            >
              <span className={`activity activity-${activity}`} />
              <span className="session-title">{session.title}</span>
              {unread > 0 ? <span className="unread">{unread}</span> : null}
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
