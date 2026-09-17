import type { VisibleStep, VisibleTool } from "./visibleTimeline";

const HINT_LIMIT = 48;

export function stepHeading(step: Pick<VisibleStep, "text" | "tools">): string {
  if (step.tools.length === 0) {
    return clip(step.text?.trim() ?? "") || "Step";
  }
  return step.tools.map(toolHeading).join(" · ");
}

function toolHeading(tool: VisibleTool): string {
  const hint = firstStringArgument(tool.arguments);
  return hint === null ? tool.name : `${tool.name}  ${hint}`;
}

function firstStringArgument(
  arguments_: Record<string, unknown>,
): string | null {
  for (const value of Object.values(arguments_)) {
    if (typeof value !== "string") {
      continue;
    }
    const text = value.trim();
    if (text) {
      return clip(text);
    }
  }
  return null;
}

function clip(text: string): string {
  if (text.length <= HINT_LIMIT) {
    return text;
  }
  return `${text.slice(0, HINT_LIMIT - 1)}…`;
}
