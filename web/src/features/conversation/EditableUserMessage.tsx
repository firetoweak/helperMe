import {
  ActionIcon,
  Button,
  Group,
  Modal,
  Paper,
  Stack,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import { IconPencil } from "@tabler/icons-react";
import { useState } from "react";

type EditableUserMessageProps = {
  text: string;
  disabled: boolean;
  saving: boolean;
  onSave: (text: string) => Promise<void>;
};

export function EditableUserMessage({
  text,
  disabled,
  saving,
  onSave,
}: EditableUserMessageProps) {
  const [opened, setOpened] = useState(false);
  const [draft, setDraft] = useState(text);

  function close() {
    if (!saving) {
      setOpened(false);
      setDraft(text);
    }
  }

  async function save() {
    const content = draft.trim();
    if (!content || content === text) {
      return;
    }
    await onSave(content);
    setOpened(false);
  }

  return (
    <>
      <Group className="user-message-row" align="flex-start" gap={6} wrap="nowrap">
        <Tooltip label={disabled ? "连接建立后可编辑" : "编辑并从这里重新执行"}>
          <ActionIcon
            aria-label="编辑消息"
            className="edit-message-button"
            disabled={disabled}
            onClick={() => setOpened(true)}
            radius="xl"
            size="sm"
            variant="subtle"
          >
            <IconPencil size={14} />
          </ActionIcon>
        </Tooltip>
        <Paper className="user-bubble" px="md" py="sm" radius="xl">
          <Text className="message-text" lh={1.6} size="sm">
            {text}
          </Text>
        </Paper>
      </Group>
      <Modal
        centered
        onClose={close}
        opened={opened}
        title="编辑消息并重新执行"
      >
        <Stack>
          <Text c="dimmed" size="xs">
            当前会话会保留；系统将在这条消息之前创建一个完整分支。
          </Text>
          <Textarea
            autosize
            autoFocus
            maxRows={12}
            minRows={4}
            onChange={(event) => setDraft(event.currentTarget.value)}
            value={draft}
          />
          <Group justify="flex-end">
            <Button disabled={saving} onClick={close} variant="subtle">
              取消
            </Button>
            <Button
              disabled={!draft.trim() || draft.trim() === text}
              loading={saving}
              onClick={() => void save()}
            >
              创建分支并执行
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
