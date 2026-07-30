// Streaming-render benchmark engine for the web token stream.
//
// Drives the REAL store pump (`pumpStreamEvents`) and the REAL bubble
// walker (`buildBubbles`) over a long transcript while a synthetic SSE
// response streams in, and measures the two costs the chat surface pays
// per streamed token:
//
//   1. React commits — every `set` that swaps the `blocks` reference
//      re-renders `ChatPage`. We count reference swaps; that is exactly
//      what drives a commit in this app.
//   2. buildBubbles CPU — `ChatPage`'s `useMemo` re-walks `blocks` into
//      bubbles on every commit. We re-run it on each commit and time it.
//
// The two optimizations under test are toggled independently so a run
// is a clean A/B of the mechanism, not of two git checkouts:
//   - `batched`: rAF-style frame coalescing in the pump (vs. one commit
//     per emitted block, which reproduces the pre-change behaviour).
//   - `incremental`: the `buildBubbles` reuse cache (vs. a full rebuild
//     each commit, which is the pre-change behaviour).
//
// `batched=false, incremental=false` is the BEFORE baseline (one commit
// per block + full rebuild). `batched=true, incremental=true` is AFTER.
//
// Pure measurement code — no React, no server. Runnable under vitest's
// jsdom env (it needs `requestAnimationFrame`, `ReadableStream`, and
// `performance.now`, all present there).

import { pumpStreamEvents, useChatStore, type FrameScheduler } from "@/store/chatStore";
import { buildBubbles, createBubbleCache, type BubbleCache } from "@/lib/renderItems";
import type { AnyBlock, BlockContext } from "@/lib/blocks";

/** Result of one benchmark run. All times are milliseconds. */
export interface BenchResult {
  /** Config echoed back for the report. */
  prefillBlocks: number;
  contentBlocks: number;
  blocksPerFrame: number;
  batched: boolean;
  incremental: boolean;
  /** React commits (blocks-reference swaps) during the streamed response. */
  commits: number;
  /** Total buildBubbles wall time across all commits. */
  buildMsTotal: number;
  /** Per-commit buildBubbles times, sorted ascending (for p50/p95). */
  perCommitMs: number[];
  /** Commit index (1-based) at which the first assistant content painted. */
  firstContentCommit: number;
  /** buildBubbles time for the commit that first painted content. */
  firstContentBuildMs: number;
}

function mkCtx(opts: { itemId?: string | null; responseId: string }): BlockContext {
  return {
    agent: "bench",
    depth: 0,
    turn: 0,
    timestamp: 0,
    responseId: opts.responseId,
    itemId: opts.itemId === undefined ? null : opts.itemId,
  };
}

/**
 * Build a finalized transcript prefix of `responses` user+assistant
 * turns (so `2 * responses` blocks), each a closed `text_done`. This is
 * the "history" already on screen before the new response streams in.
 *
 * :param responses: number of completed turns to synthesize.
 * :returns: the flat block list.
 */
function buildPrefill(responses: number): AnyBlock[] {
  const blocks: AnyBlock[] = [];
  for (let r = 0; r < responses; r += 1) {
    const rid = `pre_resp_${r}`;
    blocks.push({
      type: "user_message",
      ctx: mkCtx({ itemId: `pre_u_${r}`, responseId: rid }),
      content: [{ type: "input_text", text: `question ${r}` }],
    });
    blocks.push({
      type: "text_done",
      ctx: mkCtx({ itemId: `pre_a_${r}`, responseId: rid }),
      fullText: `Prior answer number ${r}. Some filler content for a realistic bubble.`,
      hasCodeBlocks: false,
    });
  }
  return blocks;
}

/** A writable byte stream feeding the pump's SSE parser. */
interface ByteSink {
  stream: ReadableStream<Uint8Array>;
  push: (chunk: string) => void;
  close: () => void;
}

function makeByteSink(): ByteSink {
  let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null;
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    stream,
    push(chunk) {
      ctrl!.enqueue(enc.encode(chunk));
    },
    close() {
      ctrl!.close();
    },
  };
}

/** Serialize one SSE frame the pump's parser understands. */
function sse(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** Let the async SSE-parse → reduce pipeline drain queued bytes. */
async function drain(): Promise<void> {
  for (let k = 0; k < 4; k += 1) {
    await new Promise<void>((r) => setTimeout(r, 0));
  }
}

/**
 * A `FrameScheduler` the harness fires by hand at frame boundaries, so
 * the number of commits equals the number of frames deterministically
 * (no dependence on the real rAF clock). Calls collapse onto one
 * pending callback, matching the single-flight rAF scheduler.
 */
function manualScheduler(): { scheduler: FrameScheduler; fire: () => void } {
  let pending: (() => void) | null = null;
  return {
    scheduler: {
      schedule(cb) {
        pending = cb;
      },
      cancel() {
        pending = null;
      },
    },
    fire() {
      const cb = pending;
      pending = null;
      if (cb) cb();
    },
  };
}

/**
 * A `FrameScheduler` that flushes synchronously on every schedule —
 * i.e. one commit per buffered block. Reproduces the pre-batching pump,
 * giving the BEFORE baseline through the exact same code path.
 */
function immediateScheduler(): FrameScheduler {
  return {
    schedule(cb) {
      cb();
    },
    cancel() {},
  };
}

/**
 * Run one streamed response through the real pump + renderer and report
 * the render cost.
 *
 * :param opts.prefillResponses: completed turns already on screen
 *     (transcript length = 2× this).
 * :param opts.contentDeltas: number of `output_text.delta` events the
 *     streamed response emits.
 * :param opts.blocksPerFrame: deltas delivered per coalesced frame in
 *     batched mode (ignored when `batched` is false).
 * :param opts.batched: enable rAF-style frame coalescing in the pump.
 * :param opts.incremental: enable the buildBubbles reuse cache.
 * :returns: the measured `BenchResult`.
 */
export async function runStreamRenderBench(opts: {
  prefillResponses: number;
  contentDeltas: number;
  blocksPerFrame: number;
  batched: boolean;
  incremental: boolean;
}): Promise<BenchResult> {
  const { prefillResponses, contentDeltas, blocksPerFrame, batched, incremental } = opts;
  const id = "bench-session";
  const prefill = buildPrefill(prefillResponses);

  // Seed the store as if this session were bound with `prefill` history.
  useChatStore.setState({
    conversationId: id,
    blocks: prefill,
    pendingUserMessages: [],
    activeResponse: null,
    status: "idle",
    abortController: null,
  });

  const cache: BubbleCache | undefined = incremental ? createBubbleCache() : undefined;

  let commits = 0;
  let buildMsTotal = 0;
  const perCommitMs: number[] = [];
  let firstContentCommit = 0;
  let firstContentBuildMs = 0;
  let lastBlocksRef = useChatStore.getState().blocks;

  // Mirror ChatPage: every blocks-reference swap is a commit that
  // re-walks the blocks into bubbles via useMemo.
  const unsub = useChatStore.subscribe((s) => {
    if (s.blocks === lastBlocksRef) return;
    lastBlocksRef = s.blocks;
    commits += 1;
    const t0 = performance.now();
    const bubbles = buildBubbles(s.blocks, s.activeResponse, cache);
    const dt = performance.now() - t0;
    buildMsTotal += dt;
    perCommitMs.push(dt);
    if (firstContentCommit === 0) {
      const hasContent = bubbles.some(
        (b) => b.kind === "assistant" && b.responseId === "resp_stream" && b.items.length > 0,
      );
      if (hasContent) {
        firstContentCommit = commits;
        firstContentBuildMs = dt;
      }
    }
  });

  const sink = makeByteSink();
  const controller = new AbortController();
  useChatStore.setState({ abortController: controller });
  const manual = batched ? manualScheduler() : null;
  const scheduler = manual ? manual.scheduler : immediateScheduler();

  const setState = useChatStore.setState as unknown as Parameters<typeof pumpStreamEvents>[3];
  const getState = useChatStore.getState as unknown as Parameters<typeof pumpStreamEvents>[4];
  const pumpDone = pumpStreamEvents(id, sink.stream, controller, setState, getState, scheduler);

  // Open the response.
  sink.push(sse("response.created", { id: "resp_stream", status: "in_progress", output: [] }));
  await drain();

  // Stream the text. Each delta carries a trailing space so the reducer
  // flushes a text_chunk on its 30-char threshold.
  let full = "";
  for (let n = 0; n < contentDeltas; n += 1) {
    const tok = `token${n} of streamed answer `;
    full += tok;
    sink.push(sse("response.output_text.delta", { delta: tok }));
    await drain();
    if (manual && (n + 1) % blocksPerFrame === 0) manual.fire();
  }
  if (manual) manual.fire(); // flush the partial trailing frame

  // Close the response.
  sink.push(
    sse("response.completed", {
      id: "resp_stream",
      status: "completed",
      output: [
        { type: "message", role: "assistant", content: [{ type: "output_text", text: full }] },
      ],
    }),
  );
  sink.close();
  await drain();
  await pumpDone;
  unsub();

  perCommitMs.sort((a, b) => a - b);
  const contentBlocks = useChatStore
    .getState()
    .blocks.filter((b) => b.ctx.responseId === "resp_stream").length;

  return {
    prefillBlocks: prefill.length,
    contentBlocks,
    blocksPerFrame,
    batched,
    incremental,
    commits,
    buildMsTotal,
    perCommitMs,
    firstContentCommit,
    firstContentBuildMs,
  };
}
