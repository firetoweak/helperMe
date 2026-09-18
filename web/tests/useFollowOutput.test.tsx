import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useFollowOutput } from "../src/features/conversation/useFollowOutput";

type FollowOutput = ReturnType<typeof useFollowOutput>;

class FakeResizeObserver implements ResizeObserver {
  static instances: FakeResizeObserver[] = [];

  readonly targets: Element[] = [];

  constructor(private readonly callback: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this);
  }

  observe(target: Element): void {
    this.targets.push(target);
  }

  unobserve(target: Element): void {
    const index = this.targets.indexOf(target);
    if (index >= 0) {
      this.targets.splice(index, 1);
    }
  }

  disconnect(): void {
    this.targets.length = 0;
  }

  trigger(): void {
    this.callback([], this);
  }
}

let latest!: FollowOutput;

function Harness({
  active,
  attached = true,
  sessionId = "s1",
}: {
  active: boolean;
  attached?: boolean;
  sessionId?: string;
}) {
  const follow = useFollowOutput(sessionId, active, attached);
  latest = follow;
  return (
    <div ref={follow.viewportRef} data-testid="viewport">
      <div ref={follow.contentRef} data-testid="content" />
    </div>
  );
}

type Metrics = { scrollHeight: number; clientHeight: number; scrollTop: number };

// jsdom 不做布局，手动给 viewport 装上可读写的滚动尺寸。
function installMetrics(viewport: HTMLElement, initial: Metrics): Metrics {
  const metrics = { ...initial };
  Object.defineProperty(viewport, "scrollHeight", {
    configurable: true,
    get: () => metrics.scrollHeight,
  });
  Object.defineProperty(viewport, "clientHeight", {
    configurable: true,
    get: () => metrics.clientHeight,
  });
  Object.defineProperty(viewport, "scrollTop", {
    configurable: true,
    get: () => metrics.scrollTop,
    set: (value: number) => {
      metrics.scrollTop = value;
    },
  });
  return metrics;
}

function flushResize(): void {
  act(() => {
    for (const observer of FakeResizeObserver.instances) {
      observer.trigger();
    }
  });
}

describe("useFollowOutput", () => {
  beforeEach(() => {
    FakeResizeObserver.instances = [];
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("用户上滑离开底部后，流式内容增长不再把视口拽回底部", () => {
    const { getByTestId } = render(<Harness active />);
    const viewport = getByTestId("viewport");
    const metrics = installMetrics(viewport, {
      scrollHeight: 2000,
      clientHeight: 600,
      scrollTop: 2000,
    });

    // 用户上滑到中部
    metrics.scrollTop = 500;
    fireEvent.scroll(viewport);
    expect(latest.following).toBe(false);

    // 流式内容继续增长
    metrics.scrollHeight = 2600;
    flushResize();
    expect(metrics.scrollTop).toBe(500);
  });

  it("贴着底部时，流式内容增长会跟随到底", () => {
    const { getByTestId } = render(<Harness active />);
    const viewport = getByTestId("viewport");
    const metrics = installMetrics(viewport, {
      scrollHeight: 2000,
      clientHeight: 600,
      scrollTop: 1400,
    });

    fireEvent.scroll(viewport);
    expect(latest.following).toBe(true);

    metrics.scrollHeight = 2600;
    flushResize();
    expect(metrics.scrollTop).toBe(2600);
  });

  it("scrollToBottom 重新贴底并恢复跟随", () => {
    const { getByTestId } = render(<Harness active />);
    const viewport = getByTestId("viewport");
    const metrics = installMetrics(viewport, {
      scrollHeight: 2000,
      clientHeight: 600,
      scrollTop: 500,
    });

    fireEvent.scroll(viewport);
    expect(latest.following).toBe(false);

    act(() => latest.scrollToBottom());
    expect(metrics.scrollTop).toBe(2000);
    expect(latest.following).toBe(true);
  });

  it("向上滚动的意图会立即解除跟随", () => {
    const { getByTestId } = render(<Harness active />);
    const viewport = getByTestId("viewport");
    installMetrics(viewport, { scrollHeight: 2000, clientHeight: 600, scrollTop: 1400 });

    fireEvent.scroll(viewport);
    expect(latest.following).toBe(true);

    fireEvent.wheel(viewport, { deltaY: -120 });
    expect(latest.following).toBe(false);
  });
});
