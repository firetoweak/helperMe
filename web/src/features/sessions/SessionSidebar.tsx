import {
  AppShell,
  Badge,
  Box,
  Button,
  Group,
  NavLink,
  ScrollArea,
  Skeleton,
  Stack,
  Text,
  ThemeIcon,
  Tooltip,
} from "@mantine/core";
import {
  IconMessageCircle,
  IconPlus,
  IconSparkles,
} from "@tabler/icons-react";
import { useMatch, useNavigate } from "react-router-dom";

import { useGetSessionsQuery } from "../../api/helpermeApi";
import { useAppSelector } from "../../app/hooks";
import { isForkIdentity, liveSessionId } from "../../realtime/runtimeSlice";

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
  const visibleSessions = sessions.filter(
    (session) => !isForkIdentity(session.session_id, superseded),
  );

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
        <Text c="dimmed" fw={700} fz={10} lts="0.09em" px={8} tt="uppercase">
          Sessions
        </Text>
        <ScrollArea className="session-scroll" offsetScrollbars type="hover">
          <Stack gap={3} mt="xs">
            {isLoading ? (
              <Stack gap={8} px={6}>
                <Skeleton h={38} radius="md" />
                <Skeleton h={38} radius="md" />
                <Skeleton h={38} radius="md" />
              </Stack>
            ) : null}
            {visibleSessions.map((session) => {
              const liveId = liveSessionId(session.session_id, superseded);
              const runtime = runtimes[liveId];
              const activity = runtime?.activity ?? session.activity;
              const unread = runtime?.unread ?? 0;
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
                        aria-label={activity === "running" ? "运行中" : "空闲"}
                      />
                    }
                    rightSection={
                      unread > 0 ? (
                        <Badge size="xs" variant="filled">
                          {unread}
                        </Badge>
                      ) : null
                    }
                    onClick={() => {
                      navigate(
                        `/sessions/${encodeURIComponent(session.session_id)}`,
                      );
                      onNavigate();
                    }}
                    py={9}
                    px="sm"
                    style={{ borderRadius: "var(--mantine-radius-md)" }}
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
