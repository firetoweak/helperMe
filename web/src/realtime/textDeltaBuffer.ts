export type TextDelta = {
  sessionId: string;
  outputId: string;
  text: string;
};

export function createTextDeltaBuffer(
  publish: (delta: TextDelta) => void,
  schedule: (flush: () => void) => () => void = scheduleAnimationFrame,
) {
  let pending: TextDelta | null = null;
  let cancel: (() => void) | null = null;

  function flush() {
    cancel = null;
    if (pending === null) {
      return;
    }
    const delta = pending;
    pending = null;
    publish(delta);
  }

  function enqueue(delta: TextDelta) {
    if (
      pending !== null &&
      (pending.sessionId !== delta.sessionId ||
        pending.outputId !== delta.outputId)
    ) {
      const previous = pending;
      pending = delta;
      publish(previous);
    } else if (pending !== null) {
      pending = {
        sessionId: pending.sessionId,
        outputId: pending.outputId,
        text: pending.text + delta.text,
      };
    } else {
      pending = delta;
    }
    if (cancel === null) {
      cancel = schedule(flush);
    }
  }

  function flushNow() {
    if (cancel !== null) {
      cancel();
    }
    flush();
  }

  return { enqueue, flushNow };
}

function scheduleAnimationFrame(flush: () => void): () => void {
  const id = requestAnimationFrame(flush);
  return () => cancelAnimationFrame(id);
}
