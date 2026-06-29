// Thin runner around `runStreamRenderBench` for `loadtest/web_latency.py`.
//
// Gated behind WEB_LATENCY_BENCH so the heavy 1000-block run never slows
// the normal `npm test` gate. When the env var is set it runs one
// config (chosen by WEB_LATENCY_MODE) and prints a single JSON line
// prefixed with a sentinel the Python driver greps for. It also asserts
// the optimized config is strictly better, so the bench doubles as a
// regression guard against the optimizations being undone.

import { describe, expect, it } from "vitest";
import { runStreamRenderBench, type BenchResult } from "./streamRenderBench";

// `process` isn't in web's browser TS lib; reach it through a typed
// globalThis shim so the gated runner can read env + write stdout.
const nodeProcess = (
  globalThis as unknown as {
    process?: {
      env: Record<string, string | undefined>;
      stdout: { write: (chunk: string) => void };
    };
  }
).process;
const env = nodeProcess?.env ?? {};

const BENCH_ON = env.WEB_LATENCY_BENCH === "1";
const MODE = env.WEB_LATENCY_MODE ?? "both";
const PREFILL = Number(env.WEB_LATENCY_PREFILL ?? "500");
const DELTAS = Number(env.WEB_LATENCY_DELTAS ?? "300");
const BPF = Number(env.WEB_LATENCY_BPF ?? "8");

function emit(label: string, r: BenchResult): void {
  // Sentinel-prefixed single line; the Python driver parses these.
  // process.stdout bypasses vitest's console interception so the line
  // survives to the driver verbatim.
  nodeProcess?.stdout.write(`WEB_LATENCY_JSON ${JSON.stringify({ label, ...r })}\n`);
}

describe("streaming render benchmark", () => {
  it.skipIf(!BENCH_ON)(
    "measures commits + buildBubbles cost, baseline vs optimized",
    async () => {
      const cfg = { prefillResponses: PREFILL, contentDeltas: DELTAS, blocksPerFrame: BPF };

      let baseline: BenchResult | null = null;
      let optimized: BenchResult | null = null;

      if (MODE === "baseline" || MODE === "both") {
        baseline = await runStreamRenderBench({ ...cfg, batched: false, incremental: false });
        emit("baseline", baseline);
      }
      if (MODE === "optimized" || MODE === "both") {
        optimized = await runStreamRenderBench({ ...cfg, batched: true, incremental: true });
        emit("optimized", optimized);
      }

      if (baseline && optimized) {
        // Commits must drop: the whole point of frame batching. If this
        // fails the rAF coalescing in pumpStreamEvents was removed.
        expect(optimized.commits).toBeLessThan(baseline.commits);
        // buildBubbles total CPU must drop: the incremental cache wins
        // because finalized bubbles are reused, not rebuilt.
        expect(optimized.buildMsTotal).toBeLessThan(baseline.buildMsTotal);
        // First-token paint must NOT regress: both paint content on the
        // first content commit (the pump flushes first content sync).
        expect(optimized.firstContentCommit).toBe(baseline.firstContentCommit);
      }
    },
    120_000,
  );
});
