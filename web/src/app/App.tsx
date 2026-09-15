import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";

import { useAppDispatch } from "./hooks";
import { Conversation } from "../features/conversation/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { openEventBridge } from "../realtime/eventBridge";

export function App() {
  const dispatch = useAppDispatch();

  useEffect(() => openEventBridge(dispatch), [dispatch]);

  return (
    <div className="app-shell">
      <SessionSidebar />
      <main className="main-pane">
        <Routes>
          <Route path="/" element={<EmptyState />} />
          <Route path="/sessions/:sessionId" element={<Conversation />} />
        </Routes>
      </main>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="empty-state">
      <div className="mark">H</div>
      <h1>HelperMe</h1>
      <p>选择一个 Session，或从左侧新建会话。</p>
    </div>
  );
}
