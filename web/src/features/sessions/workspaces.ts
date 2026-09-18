import type { SessionSummary } from "../../api/contracts";

/**
 * 侧栏的「工作区」视图模型。
 *
 * 后端目前还没有「会话 → 工作区」的归属字段：SessionSummary 只有
 * session_id / title / updated_at / activity（见 web/src/api/contracts.ts）。
 * 因此这里先退化为「唯一默认工作区」，把所有会话放进去，只为把分组与
 * 折叠的界面骨架立起来。
 *
 * 等后端补上归属字段后，只需要改 groupSessions 的分组依据，UI 不用动。
 */
export type WorkspaceGroup = {
  id: string;
  name: string;
  sessions: SessionSummary[];
};

export const DEFAULT_WORKSPACE_ID = "default";

export function groupSessions(
  sessions: SessionSummary[],
  defaultName = "默认工作区",
): WorkspaceGroup[] {
  if (sessions.length === 0) {
    return [];
  }
  return [
    {
      id: DEFAULT_WORKSPACE_ID,
      name: defaultName,
      sessions,
    },
  ];
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** 把 updated_at 渲染成侧栏里的相对时间；无法解析时返回空串。 */
export function formatRelativeTime(
  value: string | null,
  now: number = Date.now(),
): string {
  if (value === null) {
    return "";
  }
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) {
    return "";
  }
  const elapsed = now - timestamp;
  if (elapsed < MINUTE) {
    return "刚刚";
  }
  if (elapsed < HOUR) {
    return `${Math.floor(elapsed / MINUTE)}m`;
  }
  if (elapsed < DAY) {
    return `${Math.floor(elapsed / HOUR)}h`;
  }
  return `${Math.floor(elapsed / DAY)}d`;
}

const COLLAPSED_STORAGE_KEY = "helperme.collapsedWorkspaces";

/** 读取折叠偏好；只认值为 true 的条目，解析失败当作没有偏好。 */
export function readCollapsedWorkspaces(): Record<string, boolean> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY);
    if (raw === null) {
      return {};
    }
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const collapsed: Record<string, boolean> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (value === true) {
        collapsed[key] = true;
      }
    }
    return collapsed;
  } catch {
    return {};
  }
}

/** 折叠状态是纯界面偏好，写不进去（隐私模式等）就当作没记住。 */
export function writeCollapsedWorkspaces(
  collapsed: Record<string, boolean>,
): void {
  try {
    window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(collapsed));
  } catch {
    // 忽略：折叠偏好丢失不影响功能。
  }
}
