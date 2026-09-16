import {
  Alert,
  Box,
  Center,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconMessageCircle,
  IconSparkles,
} from "@tabler/icons-react";
import { useEffect } from "react";
import { useNavigate, useParams } from "react-router-dom";

import {
  useCancelTurnMutation,
  useEditAndForkMutation,
  useGetConversationQuery,
  useSelectSessionMutation,
  useSendInputMutation,
} from "../../api/helpermeApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import { viewing } from "../../realtime/runtimeSlice";
import { Composer } from "./Composer";
import { EditableUserMessage } from "./EditableUserMessage";
import { ExecutionProcess } from "./ExecutionProcess";
import { MarkdownMessage } from "./MarkdownMessage";
import { timelineTurns } from "./timelineTurns";
import { useFollowOutput } from "./useFollowOutput";
import { visibleTimeline } from "./visibleTimeline";

export function Conversation() {
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const { sessionId } = useParams();
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const runtime = useAppSelector((state) =>
    sessionId === undefined ? undefined : state.runtime.sessions[sessionId],
  );
  const followOutput = useFollowOutput(
    sessionId,
    runtime?.activity === "running" || (runtime?.activePreview ?? null) !== null,
  );
  const selected = useGetConversationQuery(sessionId ?? "", {
    skip: sessionId === undefined,
  });
  const [selectSession] = useSelectSessionMutation();
  const [sendInput, sending] = useSendInputMutation();
  const [editAndFork, editing] = useEditAndForkMutation();
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
    return (
      <Center h="100%">
        <Stack align="center" gap="sm">
          <Loader size="sm" />
          <Text c="dimmed" size="sm">
            正在打开 Session…
          </Text>
        </Stack>
      </Center>
    );
  }
  if (selected.isError || selected.data === undefined || sessionId === undefined) {
    const missing =
      selected.isError &&
      "status" in selected.error &&
      selected.error.status === 404;
    return (
      <Center h="100%" p="xl">
        <Alert color="red" icon={<IconAlertCircle size={18} />} title="无法打开会话">
          {missing ? "这个 Session 不存在。请从左侧新建或选择会话。" : "Session 加载失败"}
        </Alert>
      </Center>
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
  const turns = timelineTurns(items);
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

  async function edit(messageId: string, text: string) {
    if (connectionId === null) {
      return;
    }
    const fork = await editAndFork({
      connectionId,
      sessionId: currentSessionId,
      messageId,
      deliveryId: `web-${crypto.randomUUID()}`,
      text,
    }).unwrap();
    navigate(`/sessions/${fork.session_id}`);
  }

  return (
    <Box component="section" className="conversation">
      {items.length === 0 ? (
        <Center className="conversation-intro">
          <Stack align="center" gap="sm" ta="center">
            <ThemeIcon radius="xl" size={48} variant="light">
              <IconMessageCircle size={23} stroke={1.7} />
            </ThemeIcon>
            <Title order={1} fz={24} fw={650}>
              开始新的会话
            </Title>
            <Text c="dimmed" ff="monospace" fz={11}>
              {shortId(conversation.session_id)}
            </Text>
          </Stack>
        </Center>
      ) : (
        <ScrollArea
          className="timeline-scroll"
          offsetScrollbars
          type="hover"
          viewportRef={followOutput.viewportRef}
        >
          <Stack className="timeline" gap="lg" ref={followOutput.contentRef}>
            {turns.map((turn) => (
              <Stack gap="lg" key={turn.key}>
                {turn.user === null ? null : (
                  <Box component="article" className="message message-user">
                    <EditableUserMessage
                      disabled={connectionId === null}
                      onSave={(text) => edit(turn.user!.key, text)}
                      saving={editing.isLoading}
                      text={turn.user.text}
                    />
                  </Box>
                )}
                {turn.process.length === 0 ? null : (
                  <ExecutionProcess
                    complete={turn.final !== null}
                    steps={turn.process}
                  />
                )}
                {turn.active === null && turn.final === null ? null : (
                  <Box
                    component="article"
                    className="message message-assistant"
                    key={(turn.active ?? turn.final)!.key}
                  >
                    <Group align="flex-start" gap="sm" wrap="nowrap">
                      <ThemeIcon radius="xl" size={28} variant="subtle">
                        <IconSparkles size={15} />
                      </ThemeIcon>
                      <MarkdownMessage
                        content={(turn.active ?? turn.final)!.text ?? ""}
                        streaming={turn.active !== null}
                      />
                    </Group>
                  </Box>
                )}
              </Stack>
            ))}
          </Stack>
        </ScrollArea>
      )}
      <Box className="composer-dock">
        {sending.isError || editing.isError ? (
          <Alert
            className="composer-error"
            color="red"
            icon={<IconAlertCircle size={16} />}
            py="xs"
          >
            {editing.isError
              ? requestErrorMessage(
                  editing.error,
                  "消息编辑失败，未能从这条消息创建新分支。",
                )
              : "消息发送失败，请确认后端连接后重试。"}
          </Alert>
        ) : null}
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
      </Box>
    </Box>
  );
}

function shortId(id: string) {
  return `Session · ${id.slice(-8)}`;
}

function requestErrorMessage(error: unknown, fallback: string) {
  if (typeof error !== "object" || error === null || !("data" in error)) {
    return fallback;
  }
  const data = error.data;
  if (typeof data !== "object" || data === null || !("detail" in data)) {
    return fallback;
  }
  return typeof data.detail === "string" ? data.detail : fallback;
}
