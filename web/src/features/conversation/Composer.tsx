import { ActionIcon, Group, Paper, Textarea, Tooltip } from "@mantine/core";
import { IconArrowUp, IconPlayerStopFilled } from "@tabler/icons-react";
import { useState, type FormEvent, type KeyboardEvent } from "react";

type ComposerProps = {
  disabled: boolean;
  sending: boolean;
  running: boolean;
  cancelling: boolean;
  onSend: (text: string) => Promise<void>;
  onCancel: () => void;
};

export function Composer({
  disabled,
  sending,
  running,
  cancelling,
  onSend,
  onCancel,
}: ComposerProps) {
  const [text, setText] = useState("");

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const content = text.trim();
    if (content === "" || disabled || sending) {
      return;
    }
    setText("");
    await onSend(content);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) {
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <Paper
      component="form"
      className="composer"
      onSubmit={(event: FormEvent) => void submit(event)}
      p={8}
      pl="md"
      radius="xl"
      shadow="lg"
      withBorder
    >
      <Textarea
        aria-label="消息"
        autosize
        className="composer-input"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder={disabled ? "正在连接…" : "输入消息，Enter 发送"}
        minRows={1}
        maxRows={7}
        variant="unstyled"
        disabled={disabled}
      />
      <Group gap={6} wrap="nowrap">
        {running ? (
          <Tooltip label="停止当前任务">
            <ActionIcon
              aria-label="停止当前任务"
              color="red"
              disabled={cancelling}
              onClick={onCancel}
              radius="xl"
              size={38}
              type="button"
              variant="light"
            >
              <IconPlayerStopFilled size={16} />
            </ActionIcon>
          </Tooltip>
        ) : null}
        <Tooltip label="发送">
          <ActionIcon
            aria-label="发送"
            disabled={disabled || sending || text.trim() === ""}
            loading={sending}
            radius="xl"
            size={38}
            type="submit"
            variant="filled"
          >
            <IconArrowUp size={18} stroke={2.2} />
          </ActionIcon>
        </Tooltip>
      </Group>
    </Paper>
  );
}
