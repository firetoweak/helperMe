import {
  ActionIcon,
  Group,
  Paper,
  Switch,
  Text,
  Textarea,
  Tooltip,
} from "@mantine/core";
import {
  IconArrowUp,
  IconPlayerPauseFilled,
  IconPlayerPlayFilled,
  IconPlus,
} from "@tabler/icons-react";
import {
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import {
  useGetRuntimeQuery,
  useUploadAttachmentMutation,
} from "../../api/helpermeApi";
import { useAppSelector } from "../../app/hooks";
import { AttachmentTile } from "./AttachmentTile";

const ACCEPTED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

type PendingImage = {
  localId: string;
  name: string;
  previewUrl: string;
  attachmentId: string | null;
  state: "uploading" | "done" | "error";
};

type ComposerProps = {
  sessionId: string;
  connectionId: string | null;
  disabled: boolean;
  sending: boolean;
  running: boolean;
  paused: boolean;
  shouldWake: boolean;
  pauseBusy: boolean;
  retryBusy: boolean;
  autoAuthorize: boolean;
  autoAuthorizeBusy: boolean;
  onToggleAutoAuthorize: (enabled: boolean) => void;
  onSend: (text: string, artifactRefs: string[]) => Promise<void>;
  onSetPaused: (paused: boolean) => void;
  onRetry: () => void;
};

export function Composer({
  sessionId,
  connectionId,
  disabled,
  sending,
  running,
  paused,
  shouldWake,
  pauseBusy,
  retryBusy,
  autoAuthorize,
  autoAuthorizeBusy,
  onToggleAutoAuthorize,
  onSend,
  onSetPaused,
  onRetry,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState<PendingImage[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const { data: runtime } = useGetRuntimeQuery();
  const [uploadAttachment] = useUploadAttachmentMutation();
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const usage = useAppSelector(
    (state) => state.runtime.sessions[sessionId]?.contextUsage ?? null,
  );
  const used = usage?.used ?? 0;
  const limit = usage?.limit ?? runtime?.context_limit ?? 0;
  const model = runtime?.model ?? "";
  const uploading = pending.some((item) => item.state === "uploading");
  const ready = pending.filter(
    (item) => item.state === "done" && item.attachmentId !== null,
  );
  const busy = disabled || sending || uploading;
  const canSend = !busy && (text.trim() !== "" || ready.length > 0);

  useEffect(() => {
    return () => {
      for (const item of pendingRef.current) {
        URL.revokeObjectURL(item.previewUrl);
      }
    };
  }, []);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!canSend) {
      return;
    }
    const tokens = ready.map((_, index) => `[Image #${index + 1}]`);
    const content = [text.trim(), ...tokens].filter(Boolean).join(" ");
    const artifactRefs = ready.map((item) => item.attachmentId as string);
    const toRevoke = pending;
    setText("");
    setPending([]);
    try {
      await onSend(content, artifactRefs);
      for (const item of toRevoke) {
        URL.revokeObjectURL(item.previewUrl);
      }
    } catch {
      setText(content);
      setPending(toRevoke);
    }
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

  async function addAttachment(file: File) {
    if (
      connectionId === null ||
      disabled ||
      !ACCEPTED_IMAGE_TYPES.has(normalizeMime(file.type))
    ) {
      return;
    }
    const localId = crypto.randomUUID();
    const previewUrl = URL.createObjectURL(file);
    setPending((current) => [
      ...current,
      {
        localId,
        name: file.name || "image",
        previewUrl,
        attachmentId: null,
        state: "uploading",
      },
    ]);
    try {
      const uploaded = await uploadAttachment({
        connectionId,
        sessionId,
        file,
      }).unwrap();
      setPending((current) =>
        current.map((item) =>
          item.localId === localId
            ? {
                ...item,
                attachmentId: uploaded.attachment_id,
                state: "done",
              }
            : item,
        ),
      );
    } catch {
      setPending((current) =>
        current.map((item) =>
          item.localId === localId ? { ...item, state: "error" } : item,
        ),
      );
    }
  }

  function addFiles(files: File[]) {
    void Promise.all(files.map((file) => addAttachment(file)));
  }

  function onPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const files = [...(event.clipboardData?.files ?? [])];
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    addFiles(files);
  }

  function onDrop(event: DragEvent<HTMLFormElement>) {
    const files = [...event.dataTransfer.files];
    setDragging(false);
    if (files.length === 0) {
      return;
    }
    event.preventDefault();
    addFiles(files);
  }

  return (
    <Paper
      component="form"
      className="composer"
      data-dragging={dragging || undefined}
      onSubmit={(event: FormEvent) => void submit(event)}
      onDragEnter={(event) => {
        if ([...event.dataTransfer.types].includes("Files")) {
          setDragging(true);
        }
      }}
      onDragOver={(event) => {
        if ([...event.dataTransfer.types].includes("Files")) {
          event.preventDefault();
        }
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node)) {
          setDragging(false);
        }
      }}
      onDrop={onDrop}
      p={8}
      pl="md"
      radius="lg"
      shadow="lg"
      withBorder
    >
      <input
        accept="image/png,image/jpeg,image/webp,image/gif"
        hidden
        multiple
        onChange={(event) => {
          const files = [...(event.currentTarget.files ?? [])];
          event.currentTarget.value = "";
          addFiles(files);
        }}
        ref={fileRef}
        type="file"
      />
      {pending.length === 0 ? null : (
        <Group className="composer-attachments" gap={8} wrap="wrap">
          {pending.map((item) => (
            <AttachmentTile
              key={item.localId}
              name={item.name}
              onRemove={() => {
                URL.revokeObjectURL(item.previewUrl);
                setPending((current) =>
                  current.filter((entry) => entry.localId !== item.localId),
                );
              }}
              src={item.previewUrl}
              state={item.state}
            />
          ))}
        </Group>
      )}
      <Group className="composer-top" gap={10} wrap="nowrap" align="flex-end">
        <Tooltip label="添加图片，也可直接粘贴">
          <ActionIcon
            aria-label="Add attachment"
            disabled={busy || connectionId === null}
            onClick={() => fileRef.current?.click()}
            radius="xl"
            size={32}
            type="button"
            variant="subtle"
          >
            <IconPlus size={16} />
          </ActionIcon>
        </Tooltip>
        <Textarea
          aria-label="消息"
          autosize
          className="composer-input"
          value={text}
          onChange={(event) => setText(event.currentTarget.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          placeholder={disabled ? "正在连接…" : "输入消息，Enter 发送，可粘贴图片"}
          minRows={1}
          maxRows={7}
          variant="unstyled"
          disabled={disabled}
        />
        <Group gap={6} wrap="nowrap">
          {running && !paused ? (
            <Tooltip label="暂停">
              <ActionIcon
                aria-label="暂停"
                color="gray"
                disabled={pauseBusy || connectionId === null}
                onClick={() => onSetPaused(true)}
                radius="xl"
                size={36}
                type="button"
                variant="light"
              >
                <IconPlayerPauseFilled size={16} />
              </ActionIcon>
            </Tooltip>
          ) : null}
          {shouldWake && !running ? (
            <Tooltip label="继续">
              <ActionIcon
                aria-label="继续"
                color="blue"
                disabled={pauseBusy || retryBusy || connectionId === null}
                onClick={onRetry}
                radius="xl"
                size={36}
                type="button"
                variant="light"
              >
                <IconPlayerPlayFilled size={16} />
              </ActionIcon>
            </Tooltip>
          ) : null}
          <Tooltip label="发送">
            <ActionIcon
              aria-label="发送"
              disabled={!canSend}
              loading={sending}
              radius="xl"
              size={36}
              type="submit"
              variant="filled"
            >
              <IconArrowUp size={18} stroke={2.2} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>
      <Group className="composer-authorize" justify="flex-start" px="xs" py={4}>
        <Switch
          checked={autoAuthorize}
          disabled={autoAuthorizeBusy || connectionId === null}
          label="自动放行写文件等工具"
          labelPosition="left"
          onChange={(event) => onToggleAutoAuthorize(event.currentTarget.checked)}
          size="xs"
        />
      </Group>
      {runtime === undefined ? null : (
        <Group className="composer-meta" justify="space-between" wrap="nowrap">
          <Tooltip label="请求前为估算，响应后为实际输入占用">
            <Group gap={6} wrap="nowrap">
              <ContextRing used={used} limit={limit} />
              <Text c="dimmed" fz={11}>
                {formatTokens(used)} / {formatTokens(limit)}
              </Text>
            </Group>
          </Tooltip>
          <Text c="dimmed" ff="monospace" fz={11} truncate>
            {model}
          </Text>
        </Group>
      )}
    </Paper>
  );
}

function ContextRing({ used, limit }: { used: number; limit: number }) {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
  const radius = 5;
  const circumference = 2 * Math.PI * radius;
  return (
    <svg
      aria-hidden
      className="context-ring"
      height="14"
      viewBox="0 0 14 14"
      width="14"
    >
      <circle
        cx="7"
        cy="7"
        fill="none"
        r={radius}
        stroke="rgba(255, 255, 255, 0.14)"
        strokeWidth="2"
      />
      <circle
        cx="7"
        cy="7"
        fill="none"
        r={radius}
        stroke="var(--mantine-color-sage-4)"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - ratio)}
        strokeLinecap="round"
        strokeWidth="2"
        transform="rotate(-90 7 7)"
      />
    </svg>
  );
}

function formatTokens(tokens: number) {
  if (tokens < 1000) {
    return String(tokens);
  }
  if (tokens % 1000 === 0) {
    return `${tokens / 1000}k`;
  }
  return `${(tokens / 1000).toFixed(1)}k`;
}

function normalizeMime(type: string) {
  const mime = type.split(";", 1)[0].trim().toLowerCase();
  return mime === "image/jpg" ? "image/jpeg" : mime;
}
