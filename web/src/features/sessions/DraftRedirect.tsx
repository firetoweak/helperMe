import { Center, Loader, Stack, Text } from "@mantine/core";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { useCreateSessionMutation } from "../../api/helpermeApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { bindDraftSession } from "./bindDraft";

export function DraftRedirect() {
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const draftSessionId = useAppSelector((state) => state.runtime.draftSessionId);
  const [createSession] = useCreateSessionMutation();

  useEffect(() => {
    if (connectionId === null) {
      return;
    }
    if (draftSessionId !== null) {
      navigate(`/sessions/${encodeURIComponent(draftSessionId)}`, {
        replace: true,
      });
      return;
    }
    let cancelled = false;
    void bindDraftSession(connectionId, createSession, dispatch).then((id) => {
      if (!cancelled) {
        navigate(`/sessions/${encodeURIComponent(id)}`, { replace: true });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [connectionId, createSession, dispatch, draftSessionId, navigate]);

  return (
    <Center h="100%">
      <Stack align="center" gap="sm">
        <Loader size="sm" />
        <Text c="dimmed" size="sm">
          {connectionId === null ? "正在连接后端…" : "正在打开 Session…"}
        </Text>
      </Stack>
    </Center>
  );
}
