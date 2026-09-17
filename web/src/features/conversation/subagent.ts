import type { VisibleTool } from "./visibleTimeline";

/** 父 Session 上的 SubAgent 特殊工具。子过程不进父时间线，这两张卡是以后挂内容的入口。 */
export const SUBAGENT_TOOLS = ["delegate", "reclaim"] as const;

export function isSubagentTool(name: string): boolean {
  return name === "delegate" || name === "reclaim";
}

/** 当前轮次、父仍有未回收的子时，画「子 Agent 执行中」。不看 delegate 命令终态。 */
export function turnNeedsSubagentHint(latest: boolean, active: boolean): boolean {
  return latest && active;
}

export function subagentTask(tool: VisibleTool): string | null {
  const value = tool.arguments.task;
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

export function subagentChildSessionId(tool: VisibleTool): string | null {
  const value = tool.arguments.child_session_id;
  return typeof value === "string" && value !== "" ? value : null;
}
