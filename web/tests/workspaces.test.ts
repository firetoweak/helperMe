import { beforeEach, describe, expect, it } from "vitest";

import type { SessionSummary } from "../src/api/contracts";
import {
  DEFAULT_WORKSPACE_ID,
  formatRelativeTime,
  groupSessions,
  readCollapsedWorkspaces,
  writeCollapsedWorkspaces,
} from "../src/features/sessions/workspaces";

function session(sessionId: string): SessionSummary {
  return {
    session_id: sessionId,
    title: sessionId,
    updated_at: null,
    activity: "idle",
  };
}

describe("groupSessions", () => {
  it("暂时把全部会话放进唯一的默认工作区", () => {
    const groups = groupSessions([session("a"), session("b")]);

    expect(groups).toHaveLength(1);
    expect(groups[0]?.id).toBe(DEFAULT_WORKSPACE_ID);
    expect(groups[0]?.sessions.map((item) => item.session_id)).toEqual([
      "a",
      "b",
    ]);
  });

  it("没有会话时不产生空分组", () => {
    expect(groupSessions([])).toEqual([]);
  });
});

describe("formatRelativeTime", () => {
  const now = Date.parse("2026-01-02T12:00:00+00:00");

  it("缺时间或时间非法时返回空串", () => {
    expect(formatRelativeTime(null, now)).toBe("");
    expect(formatRelativeTime("不是时间", now)).toBe("");
  });

  it("按分钟/小时/天分档", () => {
    expect(formatRelativeTime("2026-01-02T11:59:30+00:00", now)).toBe("刚刚");
    expect(formatRelativeTime("2026-01-02T11:45:00+00:00", now)).toBe("15m");
    expect(formatRelativeTime("2026-01-01T22:00:00+00:00", now)).toBe("14h");
    expect(formatRelativeTime("2025-12-30T12:00:00+00:00", now)).toBe("3d");
  });
});

describe("折叠偏好", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("只记住被折叠的工作区", () => {
    writeCollapsedWorkspaces({ a: true, b: false });

    expect(readCollapsedWorkspaces()).toEqual({ a: true });
  });

  it("没有写入过时返回空对象", () => {
    expect(readCollapsedWorkspaces()).toEqual({});
  });

  it("存的内容不是对象时返回空对象", () => {
    window.localStorage.setItem("helperme.collapsedWorkspaces", "[1,2]");

    expect(readCollapsedWorkspaces()).toEqual({});
  });
});
