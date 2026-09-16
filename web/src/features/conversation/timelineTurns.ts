import type { VisibleItem, VisibleStep, VisibleUser } from "./visibleTimeline";

export type TimelineTurn = {
  key: string;
  user: VisibleUser | null;
  process: VisibleStep[];
  active: VisibleStep | null;
  final: VisibleStep | null;
};

export function timelineTurns(items: VisibleItem[]): TimelineTurn[] {
  const turns: Array<{ key: string; user: VisibleUser | null; steps: VisibleStep[] }> = [];
  let current: (typeof turns)[number] | null = null;

  for (const item of items) {
    if (item.kind === "user") {
      current = { key: item.key, user: item, steps: [] };
      turns.push(current);
      continue;
    }
    if (current === null) {
      current = { key: item.key, user: null, steps: [] };
      turns.push(current);
    }
    current.steps.push(item);
  }

  return turns.map(({ key, user, steps }) => {
    const candidate = steps.at(-1);
    const active = candidate?.pending === true ? candidate : null;
    const final = candidate !== undefined && isFinal(candidate) ? candidate : null;
    return {
      key,
      user,
      process: active === null && final === null ? steps : steps.slice(0, -1),
      active,
      final,
    };
  });
}

function isFinal(step: VisibleStep): boolean {
  return !step.pending && step.text !== null && step.tools.length === 0;
}
