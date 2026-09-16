import {
  ActionIcon,
  AppShell,
  Center,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconMenu2, IconSparkles } from "@tabler/icons-react";
import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";

import { useAppDispatch } from "./hooks";
import { Conversation } from "../features/conversation/Conversation";
import { SessionSidebar } from "../features/sessions/SessionSidebar";
import { openEventBridge } from "../realtime/eventBridge";

export function App() {
  const dispatch = useAppDispatch();
  const [navigationOpened, { close: closeNavigation, toggle: toggleNavigation }] =
    useDisclosure(false);

  useEffect(() => openEventBridge(dispatch), [dispatch]);

  return (
    <AppShell
      className="app-shell"
      navbar={{
        width: 288,
        breakpoint: "sm",
        collapsed: { mobile: !navigationOpened },
      }}
      padding={0}
      transitionDuration={180}
    >
      <AppShell.Navbar className="app-navbar" p="md">
        <SessionSidebar onNavigate={closeNavigation} />
      </AppShell.Navbar>
      <AppShell.Main className="main-pane">
        <ActionIcon
          className="mobile-nav-trigger"
          aria-label="打开会话列表"
          hiddenFrom="sm"
          onClick={toggleNavigation}
          radius="xl"
          size="lg"
          variant="default"
        >
          <IconMenu2 size={19} />
        </ActionIcon>
        <Routes>
          <Route path="/" element={<EmptyState />} />
          <Route path="/sessions/:sessionId" element={<Conversation />} />
        </Routes>
      </AppShell.Main>
    </AppShell>
  );
}

function EmptyState() {
  return (
    <Center h="100%" px="xl">
      <Stack align="center" gap="sm" ta="center">
        <ThemeIcon radius="xl" size={52} variant="light">
          <IconSparkles size={25} stroke={1.7} />
        </ThemeIcon>
        <Title order={1} fz={26} fw={650}>
          HelperMe
        </Title>
        <Text c="dimmed" maw={360} size="sm">
          选择一个会话继续，或从左侧创建新的 Session。
        </Text>
      </Stack>
    </Center>
  );
}
