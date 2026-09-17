import {
  Alert,
  Box,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconCheck,
  IconMessageCircle,
  IconSparkles,
  IconX,
} from "@tabler/icons-react";
import { useEffect } from "react";
import { useParams } from "react-router-dom";

import {
  useAuthorizeCommandMutation,
  useEditAndForkMutation,
  useGetConversationQuery,
  useSelectSessionMutation,
  useSendInputMutation,
  useSetAutoAuthorizeMutation,
  useSetPausedMutation,
  useRetryTurnMutation,
} from "../../api/helpermeApi";
import { useAppDispatch, useAppSelector } from "../../app/hooks";
import {
  authorizationResolved,
  liveSessionId,
  lockDraft,
  viewing,
} from "../../realtime/runtimeSlice";
import { Composer } from "./Composer";
import { EditableUserMessage } from "./EditableUserMessage";
import { ExecutionProcess } from "./ExecutionProcess";
import { MarkdownMessage } from "./MarkdownMessage";
import { ThinkingBlock } from "./ThinkingBlock";
import { timelineTurns, turnNeedsThinkingHint, type TimelineTurn } from "./timelineTurns";
import { useFollowOutput } from "./useFollowOutput";
import { visibleTimeline } from "./visibleTimeline";

export function Conversation() {
  const dispatch = useAppDispatch();
  const { sessionId: routeSessionId } = useParams();
  const routeId = routeSessionId ?? "";
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const ownerSessionId = useAppSelector((state) => state.runtime.ownerSessionId);
  const draftSessionId = useAppSelector((state) => state.runtime.draftSessionId);
  const superseded = useAppSelector((state) => state.runtime.supersededSessions);
  const sessionId = liveSessionId(routeId, superseded);
  const runtime = useAppSelector((state) => state.runtime.sessions[sessionId]);
  const selected = useGetConversationQuery(sessionId, {
    skip: routeSessionId === undefined,
  });
  const followOutput = useFollowOutput(
    routeSessionId,
    runtime?.activity === "running" ||
      (runtime?.activePreview ?? null) !== null ||
      (runtime?.activeThinking ?? null) !== null,
    selected.currentData !== undefined,
  );
  const [selectSession] = useSelectSessionMutation();
  const [sendInput, sending] = useSendInputMutation();
  const [editAndFork, editing] = useEditAndForkMutation();
  const [authorizeCommand, authorizing] = useAuthorizeCommandMutation();
  const [setAutoAuthorize, autoAuthorizing] = useSetAutoAuthorizeMutation();
  const [setPaused, pausing] = useSetPausedMutation();
  const [retryTurn, retrying] = useRetryTurnMutation();

  useEffect(() => {
    dispatch(viewing(sessionId === "" ? null : sessionId));
    return () => {
      dispatch(viewing(null));
    };
  }, [dispatch, sessionId]);

  useEffect(() => {
    if (connectionId === null || routeSessionId === undefined) {
      return;
    }
    if (ownerSessionId === sessionId) {
      return;
    }
    void selectSession({ connectionId, sessionId });
  }, [connectionId, ownerSessionId, routeSessionId, selectSession, sessionId]);

  const conversation = selected.currentData;
  useEffect(() => {
    if (
      conversation !== undefined &&
      conversation.session_id === draftSessionId &&
      conversation.items.some((item) => item.kind === "user")
    ) {
      dispatch(lockDraft(conversation.session_id));
    }
  }, [conversation, dispatch, draftSessionId]);
  useEffect(() => {
    if (
      routeSessionId !== undefined &&
      selected.isError &&
      "status" in selected.error &&
      selected.error.status === 404
    ) {
      dispatch(lockDraft(routeSessionId));
    }
  }, [dispatch, routeSessionId, selected.error, selected.isError]);
  if (
    conversation === undefined &&
    (selected.isLoading || selected.isUninitialized || selected.isFetching)
  ) {
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
  if (selected.isError || conversation === undefined) {
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
  const items = visibleTimeline(
    conversation,
    runtime?.committed ?? {},
    runtime?.activePreview ?? null,
    runtime?.tools ?? {},
    runtime?.committedThinking ?? {},
    runtime?.activeThinking ?? null,
  );
  const turns = timelineTurns(items);
  const running = runtime?.activity === "running";
  const lastTurnKey = turns.at(-1)?.key;

  async function send(text: string, artifactRefs: string[]) {
    if (connectionId === null) {
      throw new Error("Web connection is not active");
    }
    await sendInput({
      connectionId,
      sessionId,
      deliveryId: `web-${crypto.randomUUID()}`,
      text,
      artifactRefs,
    }).unwrap();
    dispatch(lockDraft(sessionId));
  }

  async function edit(messageId: string, text: string) {
    if (connectionId === null) {
      throw new Error("Web connection is not active");
    }
    await editAndFork({
      connectionId,
      sessionId,
      messageId,
      deliveryId: `web-${crypto.randomUUID()}`,
      text,
    }).unwrap();
  }

  async function authorize(commandId: string, approved: boolean) {
    if (connectionId === null) {
      return;
    }
    try {
      await authorizeCommand({
        connectionId,
        sessionId,
        commandId,
        approved,
      }).unwrap();
    } finally {
      dispatch(authorizationResolved({ sessionId, commandId }));
    }
  }

  async function toggleAutoAuthorize(enabled: boolean) {
    if (connectionId === null) {
      return;
    }
    await setAutoAuthorize({ connectionId, sessionId, enabled }).unwrap();
  }

  const pendingAuthorizations = Object.values(runtime?.authorizations ?? {});
  const activeAuthorization = pendingAuthorizations[0];

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
              {shortId(sessionId)}
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
            {turns.map((turn) => {
              const thinking = replyThinking(turn);
              return (
              <Stack gap="lg" key={turn.key}>
                {turn.user === null ? null : (
                  <Box component="article" className="message message-user">
                    <EditableUserMessage
                      disabled={connectionId === null}
                      images={turn.user.images}
                      onSave={(text) => edit(turn.user!.key, text)}
                      saving={editing.isLoading}
                      sessionId={sessionId}
                      text={turn.user.text}
                    />
                  </Box>
                )}
                {turn.process.length === 0 ? null : (
                  <ExecutionProcess
                    complete={turn.final !== null}
                    onAuthorize={authorize}
                    steps={turn.process}
                  />
                )}
                {thinking === null ? null : (
                  <ThinkingBlock
                    streaming={thinking.thinkingPending}
                    text={thinking.thinking}
                  />
                )}
                {turnNeedsThinkingHint(turn, {
                  latest: turn.key === lastTurnKey,
                  running,
                }) ? (
                  <Box
                    component="article"
                    className="message message-assistant"
                  >
                    <Group align="center" gap="sm" wrap="nowrap">
                      <ThemeIcon radius="xl" size={28} variant="subtle">
                        <IconSparkles size={15} />
                      </ThemeIcon>
                      <Group gap={8} wrap="nowrap">
                        <Loader color="sage" size={12} />
                        <Text c="dimmed" size="sm">
                          思考中
                        </Text>
                      </Group>
                    </Group>
                  </Box>
                ) : showReply(turn) ? (
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
                ) : null}
              </Stack>
              );
            })}
          </Stack>
        </ScrollArea>
      )}
      <Box className="composer-dock">
        {runtime?.lastError == null ? null : (
          <Alert
            className="composer-error"
            color="red"
            icon={<IconAlertCircle size={16} />}
            py="xs"
          >
            <Group gap="sm" justify="space-between" wrap="nowrap">
              <Text size="sm">{runtime.lastError}</Text>
              <Button
                disabled={connectionId === null}
                loading={retrying.isLoading}
                onClick={() => {
                  if (connectionId === null) {
                    return;
                  }
                  void retryTurn({ connectionId, sessionId }).unwrap();
                }}
                size="compact-xs"
                variant="white"
              >
                再试
              </Button>
            </Group>
          </Alert>
        )}
        {sending.isError || editing.isError || retrying.isError ? (
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
              : retrying.isError
                ? requestErrorMessage(retrying.error, "再试失败，请确认后端连接后重试。")
                : "消息发送失败，请确认后端连接后重试。"}
          </Alert>
        ) : null}
        <Composer
          sessionId={sessionId}
          connectionId={connectionId}
          disabled={connectionId === null}
          sending={sending.isLoading}
          running={running === true}
          paused={conversation.session.paused}
          shouldWake={
            conversation.session.should_wake && runtime?.lastError == null
          }
          pauseBusy={pausing.isLoading}
          autoAuthorize={conversation.session.auto_authorize}
          autoAuthorizeBusy={autoAuthorizing.isLoading}
          onToggleAutoAuthorize={toggleAutoAuthorize}
          onSend={send}
          onSetPaused={(nextPaused) => {
            if (connectionId === null) {
              return;
            }
            void setPaused({
              connectionId,
              sessionId,
              paused: nextPaused,
            }).unwrap();
          }}
        />
      </Box>
      <Modal
        centered
        closeOnClickOutside={false}
        onClose={() => {
          if (activeAuthorization !== undefined) {
            dispatch(
              authorizationResolved({
                sessionId,
                commandId: activeAuthorization.commandId,
              }),
            );
          }
        }}
        opened={activeAuthorization !== undefined}
        title="等待授权"
      >
        {activeAuthorization === undefined ? null : (
          <Stack gap="md">
            <Text ff="monospace" fw={600}>
              {activeAuthorization.name}
            </Text>
            <Text c="dimmed" className="pre-wrap" ff="monospace" fz={12}>
              {JSON.stringify(activeAuthorization.arguments, null, 2)}
            </Text>
            <Group justify="flex-end" gap="xs">
              <Button
                color="gray"
                disabled={authorizing.isLoading}
                leftSection={<IconX size={14} />}
                onClick={() => authorize(activeAuthorization.commandId, false)}
              >
                拒绝
              </Button>
              <Button
                color="sage"
                disabled={authorizing.isLoading}
                leftSection={<IconCheck size={14} />}
                loading={authorizing.isLoading}
                onClick={() => authorize(activeAuthorization.commandId, true)}
              >
                允许
              </Button>
            </Group>
          </Stack>
        )}
      </Modal>
    </Box>
  );
}

function replyThinking(turn: TimelineTurn) {
  const step = turn.active ?? turn.final;
  if (step === null || step.thinking === null) {
    return null;
  }
  return { thinking: step.thinking, thinkingPending: step.thinkingPending };
}

function showReply(turn: TimelineTurn) {
  const step = turn.active ?? turn.final;
  return step !== null && (step.text ?? "").trim() !== "";
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
