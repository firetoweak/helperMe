import {
  ActionIcon,
  AppShell,
  Badge,
  Box,
  Button,
  Collapse,
  Group,
  NavLink,
  ScrollArea,
  Skeleton,
  Stack,
  Text,
  ThemeIcon,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import {
  IconFolder,
  IconFolderPlus,
  IconMessageCircle,
  IconPlus,
  IconSparkles,
} from "@tabler/icons-react";
import { useDisclosure } from "@mantine/hooks";
import { useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";

import {
  useGetSessionsQuery,
  useGetWorkspacesQuery,
} from "../../api/helpermeApi";
import { useAppSelector } from "../../app/hooks";
import { CreateWorkspaceModal } from "./CreateWorkspaceModal";
import {
  draftSessionId,
  defaultWorkspaceId,
  formatRelativeTime,
  groupSessions,
  previewSessions,
  readCollapsedWorkspaces,
  workspaceOfSession,
  writeCollapsedWorkspaces,
} from "./workspaces";

type SessionSidebarProps = {
  onNavigate: () => void;
};

export function SessionSidebar({ onNavigate }: SessionSidebarProps) {
  const navigate = useNavigate();
  const sessionId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const draftSessions = useAppSelector((state) => state.runtime.draftSessions);
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<
    Record<string, boolean>
  >({});
  const runtimes = useAppSelector((state) => state.runtime.sessions);
  const { data: sessions = [], isLoading } = useGetSessionsQuery();
  const { data: workspaces = [], isLoading: workspacesLoading } =
    useGetWorkspacesQuery();
  const [
    createWorkspaceOpened,
    { open: openCreateWorkspace, close: closeCreateWorkspace },
  ] = useDisclosure(false);
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState(
    readCollapsedWorkspaces,
  );
  const workspaceGroups = groupSessions(sessions, workspaces);
  const fallbackWorkspaceId = defaultWorkspaceId(workspaceGroups, workspaces);
  const currentWorkspaceId = workspaceOfSession(
    sessionId,
    sessions,
    draftSessions,
  );
  const defaultDraftId = draftSessionId(draftSessions, fallbackWorkspaceId);
  const canCreateSession =
    connectionId !== null && workspaces.length > 0;

  function toggleWorkspace(workspaceId: string) {
    setCollapsedWorkspaces((current) => {
      const next = { ...current, [workspaceId]: !current[workspaceId] };
      writeCollapsedWorkspaces(next);
      return next;
    });
  }

  function openDraft(workspaceId?: string) {
    const targetId = workspaceId ?? fallbackWorkspaceId;
    if (targetId === null) {
      return;
    }
    const existing = draftSessionId(draftSessions, targetId);
    if (sessionId !== undefined && sessionId === existing) {
      onNavigate();
      return;
    }
    navigate(`/workspaces/${encodeURIComponent(targetId)}`);
    onNavigate();
  }

  return (
    <Stack h="100%" gap="md">
      <AppShell.Section>
        <Group gap="sm" px={6} py={4}>
          <ThemeIcon radius="md" size={34} variant="light">
            <IconSparkles size={18} stroke={1.8} />
          </ThemeIcon>
          <Box>
            <Text fw={700} lh={1.15} size="sm">
              HelperMe
            </Text>
            <Text c="dimmed" fz={11}>
              Personal agent
            </Text>
          </Box>
        </Group>
      </AppShell.Section>

      <AppShell.Section>
        <Button
          fullWidth
          justify="flex-start"
          leftSection={<IconPlus size={17} />}
          disabled={!canCreateSession}
          onClick={() => openDraft()}
          radius="md"
          variant={sessionId === defaultDraftId ? "filled" : "light"}
        >
          新建会话
        </Button>
      </AppShell.Section>

      <AppShell.Section grow className="session-section">
        <Group gap={4} justify="space-between" px={8} wrap="nowrap">
          <Text c="dimmed" fw={700} fz={10} lts="0.09em" tt="uppercase">
            工作区
          </Text>
          <Tooltip label="新建工作区" openDelay={400} position="left">
            <ActionIcon
              aria-label="新建工作区"
              onClick={openCreateWorkspace}
              size="sm"
              variant="subtle"
            >
              <IconFolderPlus size={15} />
            </ActionIcon>
          </Tooltip>
        </Group>
        <ScrollArea className="session-scroll" type="hover">
          {isLoading ? (
            <Stack gap={8} mt="xs" px={6}>
              <Skeleton h={38} radius="md" />
              <Skeleton h={38} radius="md" />
              <Skeleton h={38} radius="md" />
            </Stack>
          ) : null}
          <Stack gap={4} mt="xs">
            {!workspacesLoading && workspaces.length === 0 ? (
              <Box px={8} py="sm">
                <Text c="dimmed" fz={12}>
                  还没有工作区。工作区是会话的文件系统边界，先创建一个。
                </Text>
                <Button
                  fullWidth
                  mt={8}
                  onClick={openCreateWorkspace}
                  size="xs"
                  variant="light"
                >
                  新建工作区
                </Button>
              </Box>
            ) : null}
            <CreateWorkspaceModal
              onClose={closeCreateWorkspace}
              opened={createWorkspaceOpened}
            />
            {workspaceGroups.map((workspace) => {
              const collapsed = collapsedWorkspaces[workspace.id] === true;
              const expanded = expandedWorkspaces[workspace.id] === true;
              const shownSessions = previewSessions(
                workspace.sessions,
                expanded,
              );
              const hiddenCount =
                workspace.sessions.length - shownSessions.length;
              return (
                <Box
                  className={
                    workspace.id === currentWorkspaceId
                      ? "workspace-group is-current"
                      : "workspace-group"
                  }
                  key={workspace.id}
                >
                  <Group className="workspace-head" gap={0} wrap="nowrap">
                    <UnstyledButton
                      aria-expanded={!collapsed}
                      className="workspace-toggle"
                      onClick={() => toggleWorkspace(workspace.id)}
                    >
                      <Group gap={6} wrap="nowrap">
                        <IconFolder size={14} />
                        <Text
                          className="workspace-name"
                          fw={600}
                          fz={12}
                          truncate
                        >
                          {workspace.name}
                        </Text>
                      </Group>
                    </UnstyledButton>
                    <Tooltip
                      label="在此工作区新建会话"
                      openDelay={400}
                      position="right"
                    >
                      <ActionIcon
                        aria-label={`在 ${workspace.name} 新建会话`}
                        className="workspace-add"
                        disabled={connectionId === null}
                        onClick={() => openDraft(workspace.id)}
                        size="sm"
                        variant="subtle"
                      >
                        <IconPlus size={14} />
                      </ActionIcon>
                    </Tooltip>
                  </Group>
                  <Collapse expanded={!collapsed}>
                    <Stack className="workspace-sessions" gap={3}>
                      {shownSessions.map((session) => {
                        const runtime = runtimes[session.session_id];
                        const activity = runtime?.activity ?? session.activity;
                        const unread = runtime?.unread ?? 0;
                        const updatedAt = formatRelativeTime(session.updated_at);
                        return (
                          <Tooltip
                            key={session.session_id}
                            label={session.title}
                            openDelay={700}
                            position="right"
                          >
                            <NavLink
                              active={session.session_id === sessionId}
                              aria-label={session.title}
                              color="sage"
                              label={session.title}
                              leftSection={
                                <span
                                  className={`activity-dot activity-dot-${activity}`}
                                  aria-label={
                                    activity === "running" ? "运行中" : "空闲"
                                  }
                                />
                              }
                              rightSection={
                                unread > 0 ? (
                                  <Badge size="xs" variant="filled">
                                    {unread}
                                  </Badge>
                                ) : updatedAt === "" ? null : (
                                  <Text c="dimmed" fz={11}>
                                    {updatedAt}
                                  </Text>
                                )
                              }
                              onClick={() => {
                                navigate(
                                  `/sessions/${encodeURIComponent(session.session_id)}`,
                                );
                                onNavigate();
                              }}
                              py={9}
                              px="sm"
                              style={{
                                borderRadius: "var(--mantine-radius-md)",
                              }}
                              styles={{
                                label: {
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                },
                              }}
                              variant="light"
                            />
                          </Tooltip>
                        );
                      })}
                      {hiddenCount > 0 ? (
                        <UnstyledButton
                          className="workspace-more"
                          onClick={() =>
                            setExpandedWorkspaces((current) => ({
                              ...current,
                              [workspace.id]: true,
                            }))
                          }
                        >
                          <Text c="dimmed" fz={12}>
                            More · {hiddenCount}
                          </Text>
                        </UnstyledButton>
                      ) : null}
                    </Stack>
                  </Collapse>
                </Box>
              );
            })}
          </Stack>
        </ScrollArea>
      </AppShell.Section>

      <AppShell.Section>
        <Group gap="xs" px={8} py={4}>
          <IconMessageCircle size={14} />
          <Text c={connectionId === null ? "orange" : "dimmed"} fz={11}>
            {connectionId === null ? "正在连接后端…" : "实时连接已建立"}
          </Text>
        </Group>
      </AppShell.Section>
    </Stack>
  );
}
