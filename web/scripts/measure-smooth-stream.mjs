import {
  createSmoothStreamController,
  SMOOTH_STREAM_PACING_PRESETS,
} from "@ai-markdown/engine";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runCadence(name, intervalMs, chunk, count, pacing) {
  const controller = createSmoothStreamController({ pacing });
  const samples = [];
  let source = "";
  controller.subscribe(() => {
    samples.push({
      t: performance.now(),
      visible: controller.getVisible().length,
      source: source.length,
    });
  });

  const started = performance.now();
  for (let index = 0; index < count; index += 1) {
    source += chunk;
    controller.update(source);
    await sleep(intervalMs);
  }
  const afterUpdates = performance.now();
  await sleep(80);
  controller.finish();
  const drainDeadline = performance.now();
  while (!controller.isDrained() && performance.now() - drainDeadline < 2000) {
    await sleep(16);
  }
  const ended = performance.now();
  controller.dispose();

  const visibleJumps = [];
  let lastVisible = 0;
  for (const sample of samples) {
    if (sample.visible !== lastVisible) {
      visibleJumps.push({
        atMs: sample.t - started,
        delta: sample.visible - lastVisible,
        visible: sample.visible,
        source: sample.source,
        behind: sample.source - sample.visible,
      });
      lastVisible = sample.visible;
    }
  }
  const deltas = visibleJumps.map((item) => item.delta);
  return {
    name,
    pacing,
    intervalMs,
    chunkChars: chunk.length,
    count,
    updateWindowMs: afterUpdates - started,
    totalMs: ended - started,
    drained: controller.isDrained ? undefined : undefined,
    finalVisible: lastVisible,
    finalSource: source.length,
    revealUpdates: visibleJumps.length,
    revealDeltaAvg: avg(deltas),
    revealDeltaMax: Math.max(0, ...deltas),
    maxBehind: Math.max(0, ...visibleJumps.map((item) => item.behind), 0),
    jumpsOfAtLeast4: deltas.filter((delta) => delta >= 4).length,
    firstRevealMs: visibleJumps[0]?.atMs ?? null,
    lastRevealMs: visibleJumps.at(-1)?.atMs ?? null,
  };
}

function avg(values) {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
}

const presets = SMOOTH_STREAM_PACING_PRESETS;
const results = [];
for (const pacing of ["smooth", "balanced", "responsive"]) {
  results.push(await runCadence("1char/16ms", 16, "字", 40, pacing));
  results.push(await runCadence("8char/40ms", 40, "这一小段话", 20, pacing));
}

console.log(JSON.stringify({ presets, results }, null, 2));
console.log("");
console.log(
  "name            pacing       in_ms  reveal#  dlt_avg  dlt_max  max_behind  jumps>=4",
);
for (const row of results) {
  console.log(
    `${row.name.padEnd(15)} ${row.pacing.padEnd(12)} ${String(row.intervalMs).padStart(5)}  ${String(row.revealUpdates).padStart(7)}  ${row.revealDeltaAvg.toFixed(1).padStart(7)}  ${String(row.revealDeltaMax).padStart(7)}  ${String(row.maxBehind).padStart(10)}  ${String(row.jumpsOfAtLeast4).padStart(8)}`,
  );
}
