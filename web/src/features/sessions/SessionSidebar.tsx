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
  IconChevronDown,
  IconChevronRight,
  IconFolder,
  IconFolderPlus,
  IconMessageCircle,
  IconPlus,
  IconSparkles,
} from "@tabler/icons-react";
import { useState } from "react";
import { useMatch, useNavigate } from "react-router-dom";

import { useGetSessionsQuery } from "../../api/helpermeApi";
import { useAppSelector } from "../../app/hooks";
import { isForkIdentity, liveSessionId } from "../../realtime/runtimeSlice";
import {
  formatRelativeTime,
  groupSessions,
  readCollapsedWorkspaces,
  writeCollapsedWorkspaces,
} from "./workspaces";

type SessionSidebarProps = {
  onNavigate: () => void;
};

export function SessionSidebar({ onNavigate }: SessionSidebarProps) {
  const navigate = useNavigate();
  const sessionId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const connectionId = useAppSelector((state) => state.runtime.connectionId);
  const draftSessionId = useAppSelector((state) => state.runtime.draftSessionId);
  const runtimes = useAppSelector((state) => state.runtime.sessions);
  const superseded = useAppSelector((state) => state.runtime.supersededSessions);
  const { data: sessions = [], isLoading } = useGetSessionsQuery();
  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState(
    readCollapsedWorkspaces,
  );
  const visibleSessions = sessions.filter(
    (session) => !isForkIdentity(session.session_id, superseded),
  );
  const workspaceGroups = groupSessions(visibleSessions);

  function toggleWorkspace(workspaceId: string) {
    setCollapsedWorkspaces((current) => {
      const next = { ...current, [workspaceId]: !current[workspaceId] };
      writeCollapsedWorkspaces(next);
      return next;
    });
  }

  function openDraft() {
    if (sessionId !== undefined && sessionId === draftSessionId) {
      onNavigate();
      return;
    }
    if (draftSessionId !== null) {
      navigate(`/sessions/${encodeURIComponent(draftSessionId)}`);
      onNavigate();
      return;
    }
    navigate("/");
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
          disabled={connectionId === null}
          onClick={openDraft}
          radius="md"
          variant={sessionId === draftSessionId ? "filled" : "light"}
        >
          新建会话
        </Button>
      </AppShell.Section>

      <AppShell.Section grow className="session-section">
        <Group gap={4} justify="space-between" px={8} wrap="nowrap">
          <Text c="dimmed" fw={700} fz={10} lts="0.09em" tt="uppercase">
            工作区
          </Text>
          <Tooltip
            label="添加工作区尚未接入后端"
            openDelay={400}
            position="left"
          >
            <ActionIcon aria-label="添加工作区" disabled size="sm" variant="subtle">
              <IconFolderPlus size={15} />
            </ActionIcon>
          </Tooltip>
        </Group>
        <ScrollArea className="session-scroll" offsetScrollbars type="hover">
          {isLoading ? (
            <Stack gap={8} mt="xs" px={6}>
              <Skeleton h={38} radius="md" />
              <Skeleton h={38} radius="md" />
              <Skeleton h={38} radius="md" />
            </Stack>
          ) : null}
          <Stack gap={4} mt="xs">
            {workspaceGroups.map((workspace) => {
              const collapsed = collapsedWorkspaces[workspace.id] === true;
              return (
                <Box className="workspace-group" key={workspace.id}>
                  <UnstyledButton
                    aria-expanded={!collapsed}
                    className="workspace-toggle"
                    onClick={() => toggleWorkspace(workspace.id)}
                  >
                    <Group gap={6} justify="space-between" wrap="nowrap">
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
                      {collapsed ? (
                        <IconChevronRight size={14} />
                      ) : (
                        <IconChevronDown size={14} />
                      )}
                    </Group>
                  </UnstyledButton>
                  <Collapse expanded={!collapsed}>
                    <Stack className="workspace-sessions" gap={3}>
                      {workspace.sessions.map((session) => {
                        const liveId = liveSessionId(
                          session.session_id,
                          superseded,
                        );
                        const runtime = runtimes[liveId];
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
