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

function turnHasThinking(turn: TimelineTurn): boolean {
  return [turn.active, turn.final, ...turn.process].some(
    (step) => step !== null && (step.thinking ?? "").trim() !== "",
  );
}

export function turnNeedsThinkingHint(
  turn: TimelineTurn,
  options: { running: boolean; latest: boolean },
): boolean {
  if (turnHasThinking(turn)) {
    return false;
  }
  if (turn.active !== null) {
    return !(turn.active.text ?? "").trim();
  }
  if (!options.running || !options.latest || turn.final !== null) {
    return false;
  }
  return !turn.process.some((step) =>
    step.tools.some(
      (tool) =>
        tool.status === "running" || tool.status === "awaiting_authorization",
    ),
  );
}
