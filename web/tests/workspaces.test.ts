import { beforeEach, describe, expect, it } from "vitest";

import type { SessionSummary, Workspace } from "../src/api/contracts";
import {
  defaultWorkspaceId,
  draftSessionId,
  formatRelativeTime,
  groupSessions,
  previewSessions,
  readCollapsedWorkspaces,
  workspaceOfSession,
  writeCollapsedWorkspaces,
} from "../src/features/sessions/workspaces";

function session(
  sessionId: string,
  workspaceId: string,
  updatedAt: string | null = null,
): SessionSummary {
  return {
    session_id: sessionId,
    workspace_id: workspaceId,
    title: sessionId,
    updated_at: updatedAt,
    activity: "idle",
  };
}

function workspace(
  workspaceId: string,
  createdAt = "2026-01-01T00:00:00+00:00",
): Workspace {
  return {
    workspace_id: workspaceId,
    name: workspaceId,
    task_root: `/work/${workspaceId}`,
    full_access: false,
    created_at: createdAt,
  };
}

describe("groupSessions", () => {
  it("按归属把会话放进各自的工作区", () => {
    const groups = groupSessions(
      [session("a", "w1"), session("b", "w2")],
      [workspace("w1"), workspace("w2")],
    );

    expect(
      groups.map((group) => [group.id, group.sessions.map((item) => item.session_id)]),
    ).toEqual([
      ["w1", ["a"]],
      ["w2", ["b"]],
    ]);
  });

  it("最近有活动的分组排在前面，空工作区沉到后面", () => {
    const groups = groupSessions(
      [
        session("旧", "w1", "2026-01-01T00:00:00+00:00"),
        session("新", "w2", "2026-01-02T00:00:00+00:00"),
      ],
      [workspace("w1"), workspace("w2")],
    );

    expect(groups.map((group) => group.id)).toEqual(["w2", "w1"]);
  });

  it("没有会话时也列出工作区：工作区不是会话派生出来的", () => {
    expect(groupSessions([], [workspace("w1")])).toEqual([
      { id: "w1", name: "w1", sessions: [] },
    ]);
  });

  it("归属不在 registry 里时照原样列出，不静默丢会话", () => {
    const groups = groupSessions([session("a", "已消失")], []);

    expect(
      groups.map((group) => [group.id, group.sessions.map((item) => item.session_id)]),
    ).toEqual([["已消失", ["a"]]]);
  });
});

describe("defaultWorkspaceId", () => {
  it("优先落最近一次聊天的工作区", () => {
    const workspaces = [
      workspace("w1", "2026-02-01T00:00:00+00:00"),
      workspace("w2"),
    ];
    const groups = groupSessions(
      [session("a", "w2", "2026-01-02T00:00:00+00:00")],
      workspaces,
    );

    expect(defaultWorkspaceId(groups, workspaces)).toBe("w2");
  });

  it("还没有任何带归属的会话时，落最近创建的工作区", () => {
    const workspaces = [
      workspace("w1"),
      workspace("w2", "2026-02-01T00:00:00+00:00"),
    ];

    expect(defaultWorkspaceId(groupSessions([], workspaces), workspaces)).toBe(
      "w2",
    );
  });

  it("一个工作区都没有时返回 null", () => {
    expect(defaultWorkspaceId([], [])).toBeNull();
  });
});

describe("previewSessions", () => {
  it("默认只露出最近 5 条", () => {
    const sessions = ["a", "b", "c", "d", "e", "f", "g"];

    expect(previewSessions(sessions, false)).toEqual(["a", "b", "c", "d", "e"]);
    expect(previewSessions(sessions, true)).toEqual(sessions);
  });
});

describe("workspaceOfSession", () => {
  it("先看会话归属，再看未锁定草稿", () => {
    expect(
      workspaceOfSession("s1", [session("s1", "w1")], { w2: "draft" }),
    ).toBe("w1");
    expect(workspaceOfSession("draft", [], { w2: "draft" })).toBe("w2");
  });
});

describe("draftSessionId", () => {
  it("按工作区取草稿", () => {
    expect(draftSessionId({ w1: "d1" }, "w1")).toBe("d1");
    expect(draftSessionId({ w1: "d1" }, null)).toBeUndefined();
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
