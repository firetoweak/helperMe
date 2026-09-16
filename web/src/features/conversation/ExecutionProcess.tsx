import {
  Alert,
  Badge,
  Collapse,
  Group,
  Loader,
  Paper,
  Stack,
  Text,
  ThemeIcon,
  UnstyledButton,
} from "@mantine/core";
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconCode,
  IconSparkles,
} from "@tabler/icons-react";
import { useEffect, useState } from "react";

import type { ToolStatus } from "../../api/contracts";
import type { VisibleStep, VisibleTool } from "./visibleTimeline";
import { MarkdownMessage } from "./MarkdownMessage";

const STATUS_LABEL: Record<ToolStatus, string> = {
  running: "运行中",
  succeeded: "完成",
  failed: "失败",
  unknown: "中断",
};

const STATUS_COLOR: Record<ToolStatus, string> = {
  running: "sage",
  succeeded: "gray",
  failed: "red",
  unknown: "yellow",
};

interface ExecutionProcessProps {
  complete: boolean;
  steps: VisibleStep[];
}

export function ExecutionProcess({ complete, steps }: ExecutionProcessProps) {
  const [opened, setOpened] = useState(!complete);
  const running = steps.some(stepStatusIsRunning);
  const toolCount = steps.reduce((count, step) => count + step.tools.length, 0);

  useEffect(() => {
    if (complete) {
      setOpened(false);
    }
  }, [complete]);

  return (
    <Paper className="execution-process" radius="lg" withBorder>
      <UnstyledButton
        aria-expanded={opened}
        className="execution-toggle"
        onClick={() => setOpened((value) => !value)}
      >
        <Group justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <ThemeIcon color="gray" radius="md" size={30} variant="light">
              {running ? <Loader color="sage" size={14} /> : <IconSparkles size={16} />}
            </ThemeIcon>
            <div>
              <Text fw={600} size="sm">
                执行过程
              </Text>
              <Text c="dimmed" fz={11}>
                {steps.length} 个 Step · {toolCount} 次工具调用
              </Text>
            </div>
          </Group>
          {opened ? <IconChevronDown size={17} /> : <IconChevronRight size={17} />}
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>
        <Stack className="execution-steps" gap="sm">
          {steps.map((step, index) => (
            <StepDisclosure index={index} key={step.key} step={step} />
          ))}
        </Stack>
      </Collapse>
    </Paper>
  );
}

function StepDisclosure({ index, step }: { index: number; step: VisibleStep }) {
  const status = stepStatus(step);
  const [opened, setOpened] = useState(status === "running");

  return (
    <Paper className="step-panel" radius="md" withBorder>
      <UnstyledButton
        aria-expanded={opened}
        className="step-toggle"
        onClick={() => setOpened((value) => !value)}
      >
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            {opened ? <IconChevronDown size={15} /> : <IconChevronRight size={15} />}
            <Text fw={600} size="sm">
              Step {index + 1}
            </Text>
            <Text c="dimmed" fz={11}>
              {step.tools.length} 个工具
            </Text>
          </Group>
          <Badge color={STATUS_COLOR[status]} size="sm" variant="light">
            {STATUS_LABEL[status]}
          </Badge>
        </Group>
      </UnstyledButton>
      <Collapse expanded={opened}>
        <Stack className="step-content" gap="sm">
          {step.text === null ? null : (
            <MarkdownMessage content={step.text} streaming={step.pending} />
          )}
          {step.tools.map((tool) => (
            <ToolCard key={tool.commandId} tool={tool} />
          ))}
        </Stack>
      </Collapse>
    </Paper>
  );
}

function ToolCard({ tool }: { tool: VisibleTool }) {
  return (
    <Paper className="tool-card" px="md" py="sm" radius="md" withBorder>
      <Group justify="space-between" wrap="nowrap">
        <Group gap="sm" wrap="nowrap">
          <ThemeIcon
            color={STATUS_COLOR[tool.status]}
            radius="md"
            size={28}
            variant="light"
          >
            {tool.status === "running" ? (
              <Loader color="sage" size={13} />
            ) : tool.status === "failed" ? (
              <IconAlertCircle size={15} />
            ) : tool.status === "unknown" ? (
              <IconAlertTriangle size={15} />
            ) : (
              <IconCode size={15} />
            )}
          </ThemeIcon>
          <Text ff="monospace" fw={600} size="sm" truncate>
            {tool.name}
          </Text>
        </Group>
        <Badge
          color={STATUS_COLOR[tool.status]}
          leftSection={tool.status === "succeeded" ? <IconCheck size={11} /> : undefined}
          size="sm"
          variant="light"
        >
          {STATUS_LABEL[tool.status]}
        </Badge>
      </Group>
      {tool.error === null ? null : (
        <Alert color={STATUS_COLOR[tool.status]} mt="sm" py="xs" variant="light">
          <Text ff="monospace" fz={12} className="pre-wrap">
            {tool.error}
          </Text>
        </Alert>
      )}
    </Paper>
  );
}

function stepStatus(step: VisibleStep): ToolStatus {
  if (step.pending || step.tools.some((tool) => tool.status === "running")) {
    return "running";
  }
  if (step.tools.some((tool) => tool.status === "unknown")) {
    return "unknown";
  }
  if (step.tools.some((tool) => tool.status === "failed")) {
    return "failed";
  }
  return "succeeded";
}

function stepStatusIsRunning(step: VisibleStep): boolean {
  return stepStatus(step) === "running";
}
