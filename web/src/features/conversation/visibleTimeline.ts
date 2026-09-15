import type { ConversationView, ToolStatus } from "../../api/contracts";
import type { ActivePreview, LiveTool } from "../../realtime/runtimeSlice";

export type VisibleMessage = {
  key: string;
  kind: "user" | "assistant";
  text: string;
  pending: boolean;
};

export type VisibleTool = {
  key: string;
  kind: "tool";
  commandId: string;
  name: string;
  status: ToolStatus;
  error: string | null;
};

export type VisibleItem = VisibleMessage | VisibleTool;

export function visibleTimeline(
  conversation: ConversationView,
  committed: Record<string, string>,
  preview: ActivePreview | null,
  tools: Record<string, LiveTool>,
): VisibleItem[] {
  const journalOutputIds = new Set(
    conversation.items
      .filter((item) => item.kind === "assistant")
      .map((item) => item.output_id),
  );
  const journalCommandIds = new Set(
    conversation.items
      .filter((item) => item.kind === "tool")
      .map((item) => item.command_id),
  );
  const items: VisibleItem[] = conversation.items.map((item) => {
    if (item.kind === "tool") {
      const live = tools[item.command_id];
      const status = item.status === "running" ? (live?.status ?? item.status) : item.status;
      return {
        key: item.command_id,
        kind: "tool",
        commandId: item.command_id,
        name: item.name,
        status,
        error: status === "failed" ? item.error : null,
      };
    }
    return {
      key: item.message_id,
      kind: item.kind,
      text: item.text,
      pending: false,
    };
  });
  for (const live of Object.values(tools)) {
    if (journalCommandIds.has(live.commandId)) {
      continue;
    }
    items.push({
      key: live.commandId,
      kind: "tool",
      commandId: live.commandId,
      name: live.name,
      status: live.status,
      error: null,
    });
  }
  for (const [outputId, text] of Object.entries(committed)) {
    if (journalOutputIds.has(outputId)) {
      continue;
    }
    items.push({
      key: `committed:${outputId}`,
      kind: "assistant",
      text,
      pending: false,
    });
  }
  if (
    preview !== null &&
    preview.text !== "" &&
    !journalOutputIds.has(preview.outputId) &&
    committed[preview.outputId] === undefined
  ) {
    items.push({
      key: `preview:${preview.outputId}`,
      kind: "assistant",
      text: preview.text,
      pending: true,
    });
  }
  return items;
}
