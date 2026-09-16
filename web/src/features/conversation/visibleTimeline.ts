import type { ConversationView, ToolStatus } from "../../api/contracts";
import type { ActivePreview, LiveTool } from "../../realtime/runtimeSlice";

export type VisibleUser = {
  key: string;
  kind: "user";
  text: string;
  images: string[];
};

export type VisibleTool = {
  commandId: string;
  name: string;
  status: ToolStatus;
  error: string | null;
};

export type VisibleStep = {
  key: string;
  kind: "step";
  outputId: string;
  text: string | null;
  pending: boolean;
  tools: VisibleTool[];
};

export type VisibleItem = VisibleUser | VisibleStep;

export function visibleTimeline(
  conversation: ConversationView,
  committed: Record<string, string>,
  preview: ActivePreview | null,
  tools: Record<string, LiveTool>,
): VisibleItem[] {
  const journalOutputIds = new Set(
    conversation.items
      .filter((item) => item.kind === "step")
      .map((item) => item.output_id),
  );
  const items: VisibleItem[] = conversation.items.map((item) => {
    if (item.kind === "user") {
      return {
        key: item.message_id,
        kind: "user",
        text: item.text,
        images: item.images,
      };
    }
    return {
      key: `output:${item.output_id}`,
      kind: "step",
      outputId: item.output_id,
      text: item.text,
      pending: false,
      tools: item.tools.map((tool) => {
        const live = tools[tool.command_id];
        const terminal = tool.status === "succeeded" || tool.status === "failed";
        const status = terminal ? tool.status : (live?.status ?? tool.status);
        return {
          commandId: tool.command_id,
          name: tool.name,
          status,
          error: status === "failed" || status === "unknown" ? tool.error : null,
        };
      }),
    };
  });
  for (const [outputId, text] of Object.entries(committed)) {
    if (journalOutputIds.has(outputId)) {
      continue;
    }
    items.push({
      key: `output:${outputId}`,
      kind: "step",
      outputId,
      text,
      pending: true,
      tools: [],
    });
  }
  if (
    preview !== null &&
    !journalOutputIds.has(preview.outputId) &&
    committed[preview.outputId] === undefined
  ) {
    items.push({
      key: `output:${preview.outputId}`,
      kind: "step",
      outputId: preview.outputId,
      text: preview.text,
      pending: true,
      tools: [],
    });
  }
  return items;
}
