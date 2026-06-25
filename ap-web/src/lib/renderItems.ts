// Block walker — converts a flat `AnyBlock[]` into a list of bubble
// groups for the JSX layer to map over.
//
// Walks a single flat list (the same one the store holds) and groups
// by `responseId` for bubble layout. Lifecycle for the most recent
// response comes from the store's `activeResponse` sidecar.
//
// Hides four concerns the JSX layer shouldn't touch:
//   1. Splitting the flat block list into bubble groups (user
//      message → its own bubble; assistant blocks under the same
//      `responseId` → one assistant bubble; compaction → standalone).
//   2. Grouping consecutive same-kind blocks (text runs, reasoning
//      runs) into single render-items inside an assistant bubble.
//   3. Joining `tool_group` blocks with their matching `tool_result`
//      blocks by `callId` so the tool card has its output inline.
//   4. Deriving the tool's UI status from block fields plus the
//      bubble's lifecycle (running / completed / errored / cancelled).
//
// Pure function. No React, no DOM. Tested in `renderItems.test.ts`.

import type { AnyBlock, MessageContentBlock, ToolExecution, ToolResultBlock } from "./blocks";
import type { RememberScope } from "./types";
import type { ActiveResponse } from "@/store/types";

/**
 * UI states for a tool card. The first three line up with values in
 * `ToolUIPart["state"]` from the `ai` package and pass through to the
 * vendored `<ToolHeader>`. The fourth ("cancelled") is our own —
 * rendered by the `<ToolCard>` wrapper because the vendored type union
 * doesn't include it.
 */
export type ToolState =
  | "input-available" // running (output is null, no error)
  | "output-available" // completed (output present)
  | "output-error" // turn-level error happened during this tool
  | "cancelled" // turn was cancelled while output was still null
  | "no-output"; // turn finished (completed/incomplete) but no result was ever recorded

/** A single rendered item inside an assistant bubble. */
export type RenderItem =
  | { kind: "text"; itemId: string | null; text: string; final: boolean }
  | {
      kind: "reasoning";
      itemId: string | null;
      text: string;
      duration: number | undefined;
    }
  | {
      kind: "tool";
      itemId: string | null;
      execution: ToolExecution;
      output: string | null;
      state: ToolState;
      startedAt: number | null;
      duration: number | undefined;
    }
  | {
      kind: "native_tool";
      itemId: string | null;
      toolType: string;
      label: string;
      data: Record<string, unknown>;
    }
  | {
      kind: "slash_command";
      itemId: string | null;
      /** `"skill"` for Skills, `"command"` for surfaced CLI built-ins. */
      slashKind: "skill" | "command";
      name: string;
      arguments: string;
      output: string | null;
    }
  | {
      kind: "terminal_command";
      itemId: string | null;
      terminalKind: "input" | "output";
      input: string | null;
      stdout: string | null;
      stderr: string | null;
    }
  | { kind: "policy_denied"; itemId: string | null; reason: string; phase: string }
  | { kind: "error"; itemId: string | null; message: string; source: string; code: string }
  | {
      kind: "retry";
      itemId: string | null;
      source: string;
      attempt: number;
      maxAttempts: number;
      delaySeconds: number;
    }
  | {
      kind: "elicitation";
      itemId: string | null;
      elicitationId: string;
      targetSessionId?: string | null;
      message: string;
      phase: string;
      policyName: string;
      contentPreview: string;
      requestedSchema: Record<string, unknown>;
      url?: string | null;
      status: "pending" | "responded";
      response: {
        action: "accept" | "decline" | "cancel" | "auto_resolved";
        content?: Record<string, unknown>;
      } | null;
      askUserQuestion?: Record<string, unknown> | null;
      exitPlanMode?: Record<string, unknown> | null;
      codexCommand?: {
        command: string;
        cwd: string | null;
        reason: string | null;
        execPolicyAmendment: string[] | null;
      } | null;
      allowAllEdits?: boolean;
      rememberScope?: RememberScope | null;
    };

/** A bubble cluster. The page maps over these. */
export type Bubble =
  | {
      kind: "user";
      itemId: string;
      content: MessageContentBlock[];
      /** Human author email, when known. */
      createdBy?: string;
      /**
       * Stable React key when promoted from an optimistic
       * `pendingUserMessages` entry — carries that entry's client temp
       * id so the bubble keeps the same key across the
       * optimistic→committed swap (no remount/flink).
       */
      stableKey?: string;
    }
  | {
      kind: "assistant";
      responseId: string;
      // Stable, sibling-unique identifier for React keying
      stableId: string;
      lifecycle: ActiveResponse["state"];
      /** Free-form error message when `lifecycle === "failed"`. */
      error: string | null;
      items: RenderItem[];
    }
  | { kind: "compaction_loading"; itemId: string }
  | { kind: "compaction"; itemId: string }
  | {
      kind: "routing_decision";
      itemId: string;
      model: string;
      tier: "cheap" | "medium" | "expensive";
      applied: boolean;
      rationale: string;
    };

const TEXT_BLOCK_TYPES = new Set(["text_chunk", "text_done"]);
const REASONING_BLOCK_TYPES = new Set(["reasoning_start", "reasoning_chunk", "reasoning_block"]);

/**
 * Cross-call reuse cache for `buildBubbles`.
 *
 * Streaming appends to `blocks` once per frame; every prior bubble is
 * finalized and would otherwise be rebuilt (new objects, re-walked
 * blocks) on each append. The cache lets `buildBubbles` reuse the
 * finalized prefix *by reference* and rebuild only the active (last)
 * bubble, so the per-frame cost is O(active bubble) instead of
 * O(whole transcript). Callers hold one instance per chat surface
 * (e.g. a `useRef`) and pass it on every call; it self-heals (full
 * rebuild) whenever the new inputs aren't an append-only extension of
 * the cached ones — so a session switch, history prepend, or in-place
 * block edit is always correct, just not incremental.
 *
 * :param blocks: the `blocks` array the cached `bubbles` were built
 *     from, kept for identity + per-element reference comparison.
 * :param activeResponse: the `activeResponse` the cached `bubbles`
 *     were built with.
 * :param bubbles: the last result returned.
 * :param lastBubbleStart: block index where the last (active) bubble's
 *     group started, i.e. the only region that can change on append.
 *     ``-1`` when there were no bubbles.
 */
export interface BubbleCache {
  blocks: AnyBlock[] | null;
  activeResponse: ActiveResponse | null;
  interruptedResponseIds: readonly string[] | null;
  bubbles: Bubble[];
  lastBubbleStart: number;
}

const EMPTY_INTERRUPTED_RESPONSE_IDS: readonly string[] = [];

/** A fresh, empty `BubbleCache`. */
export function createBubbleCache(): BubbleCache {
  return {
    blocks: null,
    activeResponse: null,
    interruptedResponseIds: null,
    bubbles: [],
    lastBubbleStart: -1,
  };
}

/**
 * Walk a flat block list and produce the bubble cluster list.
 *
 * @param blocks - the conversation's blocks in arrival order. Includes
 *   `UserMessageBlock`s (from `itemsToBlocks` history hydration or
 *   from optimistic-insert at send time), assistant-side blocks
 *   (text/reasoning/tool/native_tool/error/retry), and `CompactionBlock`s.
 *   Lifecycle markers (`response_start`, `response_end`) are skipped.
 * @param activeResponse - lifecycle of the most recently sent response,
 *   or `null` when idle / pre-send. The bubble whose `responseId`
 *   matches inherits `state` and `error`; all other bubbles are
 *   `"completed"`.
 * @param cache - optional reuse cache (see `BubbleCache`). When supplied
 *   and the call is an append-only extension of the cached one, the
 *   finalized-bubble prefix is reused by reference and only the active
 *   bubble is rebuilt. Omitted (the default) → a pure full rebuild with
 *   fresh object identities, which is what every non-streaming caller
 *   and the unit tests rely on.
 * @param interruptedResponseIds - response ids whose bubbles should remain
 *   labelled cancelled even after the active response sidecar has moved on.
 */
export function buildBubbles(
  blocks: AnyBlock[],
  activeResponse: ActiveResponse | null,
  cache?: BubbleCache,
  interruptedResponseIds: readonly string[] = EMPTY_INTERRUPTED_RESPONSE_IDS,
): Bubble[] {
  const interruptedResponses = new Set(interruptedResponseIds);
  if (cache === undefined) {
    return walkBubbles(blocks, activeResponse, interruptedResponses, 0, [], new Map()).bubbles;
  }

  // Nothing changed since the last call — hand back the same array.
  if (
    cache.blocks === blocks &&
    cache.activeResponse === activeResponse &&
    cache.interruptedResponseIds === interruptedResponseIds
  ) {
    return cache.bubbles;
  }

  // Try the incremental path: reuse every finalized bubble (all but the
  // last) and rebuild only from where the last cached bubble started.
  const reuse = reusablePrefix(blocks, activeResponse, interruptedResponses, cache);
  if (reuse !== null) {
    const subIndexSeed = new Map<string, number>();
    for (const b of reuse.prefix) {
      if (b.kind === "assistant") {
        subIndexSeed.set(b.responseId, (subIndexSeed.get(b.responseId) ?? 0) + 1);
      }
    }
    const rest = walkBubbles(
      blocks,
      activeResponse,
      interruptedResponses,
      reuse.startBlock,
      reuse.prefix,
      subIndexSeed,
    );
    cache.blocks = blocks;
    cache.activeResponse = activeResponse;
    cache.interruptedResponseIds = interruptedResponseIds;
    cache.bubbles = rest.bubbles;
    cache.lastBubbleStart = rest.lastBubbleStart;
    return rest.bubbles;
  }

  // Cache miss (session switch, history prepend, in-place edit) — full rebuild.
  const full = walkBubbles(blocks, activeResponse, interruptedResponses, 0, [], new Map());
  cache.blocks = blocks;
  cache.activeResponse = activeResponse;
  cache.interruptedResponseIds = interruptedResponseIds;
  cache.bubbles = full.bubbles;
  cache.lastBubbleStart = full.lastBubbleStart;
  return full.bubbles;
}

/**
 * Decide whether `cache` can be reused as an append-only prefix of
 * the current `(blocks, activeResponse)`, and if so return the
 * reusable bubble prefix plus the block index to resume walking from.
 *
 * Reuse is valid only when (1) every block before the last cached
 * bubble is reference-identical in the new array — so an in-place edit
 * or history prepend forces a full rebuild — and (2) none of the
 * reused bubbles' lifecycle depends on `activeResponse` (i.e. no reused
 * assistant bubble matches the active response id), since that bubble's
 * rendered state could otherwise change without its blocks changing.
 *
 * :returns: ``{prefix, startBlock}`` when reuse is safe, else ``null``.
 */
function reusablePrefix(
  blocks: AnyBlock[],
  activeResponse: ActiveResponse | null,
  interruptedResponses: ReadonlySet<string>,
  cache: BubbleCache,
): { prefix: Bubble[]; startBlock: number } | null {
  if (cache.blocks === null || cache.bubbles.length === 0 || cache.lastBubbleStart <= 0) {
    return null;
  }
  const startBlock = cache.lastBubbleStart;
  // The new array must be at least as long, and the finalized prefix
  // region must be byte-for-byte (reference-for-reference) unchanged.
  if (blocks.length < startBlock) return null;
  for (let j = 0; j < startBlock; j += 1) {
    if (blocks[j] !== cache.blocks[j]) return null;
  }
  // A tool_result appended since the last walk whose call has no
  // tool_group in the rewalk region folds into a FINALIZED prefix
  // bubble's card — reuse would render that card without its output.
  // Older region results were folded by the rebuild that admitted them.
  // Rid-scoped: a backdated result for a reused callId targets the prefix, not the live group.
  const regionCallIds = new Set<string>();
  for (let j = startBlock; j < blocks.length; j += 1) {
    const blk = blocks[j]!;
    if (blk.type !== "tool_group") continue;
    for (const ex of blk.executions) regionCallIds.add(`${blk.ctx.responseId}:${ex.callId}`);
  }
  for (let j = Math.max(startBlock, cache.blocks.length); j < blocks.length; j += 1) {
    const blk = blocks[j]!;
    if (blk.type === "tool_result" && !regionCallIds.has(`${blk.ctx.responseId}:${blk.callId}`)) {
      return null;
    }
  }
  const prefix = cache.bubbles.slice(0, cache.bubbles.length - 1);
  // A reused assistant bubble whose response is still the active one
  // could change lifecycle/error without a block change — don't reuse.
  const activeId = activeResponse?.responseId;
  if (activeId !== undefined) {
    for (const b of prefix) {
      if (b.kind === "assistant" && b.responseId === activeId) return null;
    }
  }
  for (const b of prefix) {
    if (b.kind === "assistant" && interruptedResponses.has(b.responseId)) {
      return null;
    }
  }
  return { prefix, startBlock };
}

/**
 * The core block-walking loop, shared by the full and incremental
 * paths. Starts at `startIndex`, appends bubbles onto a copy of
 * `seedBubbles` (the reused finalized prefix, or `[]`), and reports
 * where the final bubble's group started so the cache can resume there
 * next time.
 *
 * :param startIndex: block index to begin walking from.
 * :param seedBubbles: already-built bubbles to prepend (reused by
 *     reference); copied, not mutated.
 * :param subIndexByResp: seeded count of assistant bubbles already
 *     emitted per responseId, so streaming-only bubbles (no itemId)
 *     keep stable `stableId`s across the reuse boundary.
 * :returns: the bubble list and the block index where the last bubble
 *     began (``-1`` if no bubbles were produced at all).
 */
function walkBubbles(
  blocks: AnyBlock[],
  activeResponse: ActiveResponse | null,
  interruptedResponses: ReadonlySet<string>,
  startIndex: number,
  seedBubbles: Bubble[],
  subIndexByResp: Map<string, number>,
): { bubbles: Bubble[]; lastBubbleStart: number } {
  const bubbles: Bubble[] = [...seedBubbles];
  // One cross-bubble result index per walk: the relay backdates a
  // delayed function_call_output to its ORIGINAL turn's response id, so
  // a result can sit outside its call's bubble; pairing is keyed
  // (responseId, callId) across the walked range so a reused callId
  // can't adopt another turn's output. `reusablePrefix` rejects reuse
  // when a new result would target the prefix, so the range always
  // covers the pair.
  const crossBubbleResults = new Map<string, ToolResultBlock>();
  for (let j = startIndex; j < blocks.length; j += 1) {
    const blk = blocks[j]!;
    if (blk.type === "tool_result") {
      crossBubbleResults.set(`${blk.ctx.responseId}:${blk.callId}`, blk);
    }
  }
  // Block index where the most recently pushed bubble's group started.
  let lastBubbleStart = bubbles.length > 0 ? 0 : -1;
  let i = startIndex;

  while (i < blocks.length) {
    const b = blocks[i]!;

    // Lifecycle markers don't render — they exist for the streaming
    // reducer and the eager URL update, not the renderer.
    if (b.type === "response_start" || b.type === "response_end") {
      i += 1;
      continue;
    }

    if (b.type === "user_message") {
      lastBubbleStart = i;
      bubbles.push({
        kind: "user",
        itemId: b.ctx.itemId ?? `user_${i}`,
        content: b.content,
        ...(b.ctx.createdBy !== undefined ? { createdBy: b.ctx.createdBy } : {}),
        // Carry the optimistic temp id (when promoted) so bubbleKey holds
        // steady across the optimistic→committed swap — no remount/flink.
        stableKey: b.stableKey,
      });
      i += 1;
      continue;
    }

    if (b.type === "compaction_loading") {
      lastBubbleStart = i;
      bubbles.push({
        kind: "compaction_loading",
        itemId: b.ctx.itemId ?? `compaction_loading_${i}`,
      });
      i += 1;
      continue;
    }

    if (b.type === "compaction") {
      // If the immediately preceding bubble is a loading spinner for
      // this same compaction, replace it with the done marker so the
      // user sees a single entry transition from spinner → checkmark.
      if (bubbles.length > 0 && bubbles[bubbles.length - 1]?.kind === "compaction_loading") {
        bubbles.pop();
      }
      lastBubbleStart = i;
      bubbles.push({
        kind: "compaction",
        itemId: b.ctx.itemId ?? `compaction_${i}`,
      });
      i += 1;
      continue;
    }

    if (b.type === "routing_decision") {
      // Standalone muted chip at its transcript position (turn start),
      // never folded into an adjacent assistant bubble.
      lastBubbleStart = i;
      bubbles.push({
        kind: "routing_decision",
        itemId: b.ctx.itemId ?? `routing_${i}`,
        model: b.model,
        tier: b.tier,
        applied: b.applied,
        rationale: b.rationale,
      });
      i += 1;
      continue;
    }

    // Open an assistant bubble. Collect all subsequent blocks that
    // share this `responseId` and are not themselves user/compaction
    // boundaries. Out-of-band blocks with a different responseId
    // (shouldn't happen in well-formed streams, but be defensive)
    // close the bubble.
    const groupResponseId = b.ctx.responseId;
    const groupStart = i;
    while (i < blocks.length) {
      const cur = blocks[i]!;
      // Break on boundaries that start a new top-level bubble. Include
      // `compaction_loading` so in-progress compaction spinners are not
      // silently absorbed into the preceding assistant bubble (they share
      // the previous response's `responseId` because the BlockStream
      // carries `state.responseId` into `ctx()` for all events).
      if (
        cur.type === "user_message" ||
        cur.type === "compaction" ||
        cur.type === "compaction_loading" ||
        cur.type === "routing_decision"
      )
        break;
      if (cur.type === "response_start" || cur.type === "response_end") {
        i += 1;
        continue;
      }
      // A bare tool_result never renders standalone (it folds into its
      // call's card by callId) — don't let a backdated one split the
      // open bubble.
      if (cur.ctx.responseId !== groupResponseId && cur.type !== "tool_result") break;
      i += 1;
    }
    const groupBlocks = blocks.slice(groupStart, i).filter(isAssistantSideBlock);
    // A group of only tool_results renders nothing itself — skip it so
    // an orphan late output doesn't paint an empty assistant bubble.
    if (groupBlocks.length > 0 && groupBlocks.every((bk) => bk.type === "tool_result")) {
      continue;
    }
    const lifecycle =
      groupHasInterruptedText(groupBlocks) || interruptedResponses.has(groupResponseId)
        ? "cancelled"
        : activeResponse?.responseId === groupResponseId
          ? activeResponse.state
          : "completed";
    const error = activeResponse?.responseId === groupResponseId ? activeResponse.error : null;

    const subIndex = subIndexByResp.get(groupResponseId) ?? 0;
    subIndexByResp.set(groupResponseId, subIndex + 1);
    // Absorbed tool_results don't key the bubble: a backdated one would flip stableId mid-stream.
    const firstItemId =
      groupBlocks.find((bk) => bk.type !== "tool_result" && bk.ctx.itemId !== null)?.ctx.itemId ??
      null;
    const stableId = firstItemId ?? `${groupResponseId}:${subIndex}`;

    lastBubbleStart = groupStart;
    bubbles.push({
      kind: "assistant",
      responseId: groupResponseId,
      stableId,
      lifecycle,
      error,
      items: buildAssistantItems(groupBlocks, lifecycle, crossBubbleResults),
    });
  }

  return { bubbles, lastBubbleStart };
}

/** Filter to blocks that participate in assistant rendering. */
function isAssistantSideBlock(b: AnyBlock): boolean {
  return (
    b.type !== "user_message" &&
    b.type !== "compaction" &&
    // compaction_loading has its own top-level bubble slot and must not
    // end up in an assistant bubble's item list.
    b.type !== "compaction_loading" &&
    // routing_decision is a standalone top-level chip, not an assistant item.
    b.type !== "routing_decision" &&
    b.type !== "response_start" &&
    b.type !== "response_end" &&
    // FileBlock is currently deferred from rendering; skip silently.
    b.type !== "file"
  );
}

/** Return true when persisted text marks the assistant turn interrupted. */
function groupHasInterruptedText(blocks: AnyBlock[]): boolean {
  return blocks.some((b) => b.type === "text_done" && b.interrupted === true);
}

/**
 * Walk an assistant bubble's blocks and produce its `RenderItem[]`.
 *
 * Implements: text-run grouping with trailing-empty-message dedup,
 * reasoning-run grouping, tool↔result pairing via the walk-wide
 * `crossBubbleResults` index keyed `(responseId, callId)` — covering
 * this bubble's own results and delayed cross-turn outputs alike,
 * while a reused callId can never adopt another turn's output —
 * and tool-state derivation from bubble lifecycle plus the trailing
 * live tool phase.
 */
function buildAssistantItems(
  groupBlocks: AnyBlock[],
  lifecycle: ActiveResponse["state"],
  crossBubbleResults: Map<string, ToolResultBlock>,
): RenderItem[] {
  // Results render only by folding into a call's card — strip them so
  // an absorbed out-of-band result can't split a text/reasoning run.
  const blocks = groupBlocks.filter((b) => b.type !== "tool_result");
  const liveToolCallIds = trailingLiveToolCallIds(blocks, lifecycle);

  // Pre-compute: is there any non-empty TextDone in this bubble?
  // Used to drop trailing-empty assistant messages — the server
  // emits one real-text + one empty trailing message item per
  // response, and the empty one would otherwise render as a blank
  // bubble line.
  const hasNonEmptyTextDone = blocks.some((b) => b.type === "text_done" && b.fullText.length > 0);

  const items: RenderItem[] = [];
  let i = 0;
  while (i < blocks.length) {
    const b = blocks[i]!;

    // Group consecutive text blocks into runs, splitting each run by
    // `text_done` boundaries. Each `text_done` ends one logical
    // message; trailing chunks without a `text_done` form an
    // in-progress tail.
    if (TEXT_BLOCK_TYPES.has(b.type)) {
      const start = i;
      while (i < blocks.length && TEXT_BLOCK_TYPES.has(blocks[i]!.type)) i += 1;
      const run = blocks.slice(start, i);
      let chunkStart = 0;
      for (let k = 0; k < run.length; k += 1) {
        if (run[k]!.type === "text_done") {
          const slice = run.slice(chunkStart, k + 1);
          const item = textItem(slice);
          // Drop empty trailing TextDones when a non-empty one exists
          // in the same bubble. Without this, the server's empty
          // trailing message item would render as a blank bubble line.
          // textItem() always returns a `text` kind for text-block runs,
          // but TS doesn't narrow on the helper's return type — assert.
          if (item.kind !== "text") continue;
          if (!(item.text === "" && item.final && hasNonEmptyTextDone)) {
            items.push(item);
          }
          chunkStart = k + 1;
        }
      }
      if (chunkStart < run.length) {
        items.push(textItem(run.slice(chunkStart)));
      }
      continue;
    }

    // Group consecutive reasoning blocks into one reasoning run.
    if (REASONING_BLOCK_TYPES.has(b.type)) {
      const start = i;
      while (i < blocks.length && REASONING_BLOCK_TYPES.has(blocks[i]!.type)) i += 1;
      items.push(reasoningItem(blocks.slice(start, i)));
      continue;
    }

    if (b.type === "tool_group") {
      // Each tool_group can contain multiple ToolExecutions today (the
      // reducer only ever emits one per group, but the schema permits
      // many — preserve that shape).
      for (const ex of b.executions) {
        items.push(
          toolItem(
            ex,
            b.ctx.itemId,
            b.ctx.timestamp,
            // Rid-scoped lookup: a reused callId can't adopt another turn's output.
            crossBubbleResults.get(`${b.ctx.responseId}:${ex.callId}`),
            lifecycle,
            liveToolCallIds.has(ex.callId),
          ),
        );
      }
      i += 1;
      continue;
    }

    if (b.type === "native_tool") {
      items.push({
        kind: "native_tool",
        itemId: b.ctx.itemId,
        toolType: b.toolType,
        label: b.label,
        data: b.data,
      });
      i += 1;
      continue;
    }

    if (b.type === "slash_command") {
      items.push({
        kind: "slash_command",
        itemId: b.ctx.itemId,
        slashKind: b.kind,
        name: b.name,
        arguments: b.arguments,
        output: b.output,
      });
      i += 1;
      continue;
    }

    if (b.type === "terminal_command") {
      items.push({
        kind: "terminal_command",
        itemId: b.ctx.itemId,
        terminalKind: b.kind,
        input: b.input,
        stdout: b.stdout,
        stderr: b.stderr,
      });
      i += 1;
      continue;
    }

    if (b.type === "policy_denied") {
      items.push({
        kind: "policy_denied",
        itemId: b.ctx.itemId,
        reason: b.reason,
        phase: b.phase,
      });
      i += 1;
      continue;
    }

    if (b.type === "error") {
      items.push({
        kind: "error",
        itemId: b.ctx.itemId,
        message: b.message,
        source: b.source,
        code: b.code,
      });
      i += 1;
      continue;
    }

    if (b.type === "retry") {
      items.push({
        kind: "retry",
        itemId: b.ctx.itemId,
        source: b.source,
        attempt: b.attempt,
        maxAttempts: b.maxAttempts,
        delaySeconds: b.delaySeconds,
      });
      i += 1;
      continue;
    }

    if (b.type === "elicitation") {
      items.push({
        kind: "elicitation",
        itemId: b.ctx.itemId,
        elicitationId: b.elicitationId,
        targetSessionId: b.targetSessionId,
        message: b.message,
        phase: b.phase,
        policyName: b.policyName,
        contentPreview: b.contentPreview,
        requestedSchema: b.requestedSchema,
        url: b.url,
        status: b.status,
        response: b.response,
        askUserQuestion: b.askUserQuestion,
        exitPlanMode: b.exitPlanMode,
        codexCommand: b.codexCommand,
        allowAllEdits: b.allowAllEdits,
        rememberScope: b.rememberScope,
      });
      i += 1;
      continue;
    }

    // Defensive default — shouldn't fire because every block type is
    // handled above. If we ever add a new block type to `blocks.ts`
    // without updating this file, surface it visibly so future-us
    // notices instead of silently dropping data.
    i += 1;
  }

  return items;
}

function trailingLiveToolCallIds(
  blocks: AnyBlock[],
  lifecycle: ActiveResponse["state"],
): Set<string> {
  const callIds = new Set<string>();
  if (lifecycle !== "streaming") return callIds;

  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const b = blocks[i]!;
    if (b.type === "tool_result") continue;
    if (b.type === "native_tool") continue;
    if (b.type !== "tool_group") break;
    for (const ex of b.executions) {
      callIds.add(ex.callId);
    }
  }
  return callIds;
}

function textItem(run: AnyBlock[]): RenderItem {
  // Prefer the canonical fullText from text_done if present; otherwise
  // concatenate the text_chunks. Use the text_done's itemId for keys
  // when available.
  for (let i = run.length - 1; i >= 0; i -= 1) {
    const b = run[i]!;
    if (b.type === "text_done") {
      return {
        kind: "text",
        itemId: b.ctx.itemId,
        text: b.fullText,
        final: true,
      };
    }
  }
  const text = run
    .filter((b) => b.type === "text_chunk")
    .map((b) => (b as Extract<AnyBlock, { type: "text_chunk" }>).text)
    .join("");
  return { kind: "text", itemId: null, text, final: false };
}

function reasoningItem(run: AnyBlock[]): RenderItem {
  // Duration: span between the first and last block in the reasoning
  // run. Blocks carry monotonic timestamps from `performance.now()`.
  // Historical blocks (from `itemsToBlocks`) have `timestamp = 0`, so
  // the diff is 0 there — surface as undefined so the renderer hides
  // the number.
  const first = run[0];
  const last = run[run.length - 1];
  const duration =
    first && last && first.ctx.timestamp > 0 && last.ctx.timestamp > first.ctx.timestamp
      ? last.ctx.timestamp - first.ctx.timestamp
      : undefined;

  // If chunks are present, concatenate them. Otherwise fall back to a
  // closed reasoning_block's reasoningText / summaryText.
  const chunkText = run
    .filter((b) => b.type === "reasoning_chunk")
    .map((b) => (b as Extract<AnyBlock, { type: "reasoning_chunk" }>).text)
    .join("");
  if (chunkText.length > 0) {
    return { kind: "reasoning", itemId: null, text: chunkText, duration };
  }
  for (let i = run.length - 1; i >= 0; i -= 1) {
    const b = run[i]!;
    if (b.type === "reasoning_block") {
      const parts = [b.reasoningText, b.summaryText].filter((s) => s.length > 0);
      return {
        kind: "reasoning",
        itemId: null,
        text: parts.join("\n\n"),
        duration,
      };
    }
  }
  // reasoning_start with nothing else — empty section.
  return { kind: "reasoning", itemId: null, text: "", duration };
}

function toolItem(
  execution: ToolExecution,
  itemId: string | null,
  startedAtTimestamp: number,
  result: ToolResultBlock | undefined,
  lifecycle: ActiveResponse["state"],
  isLiveToolCall: boolean,
): RenderItem {
  const output = result?.output ?? execution.output ?? null;
  const startedAt = startedAtTimestamp > 0 ? startedAtTimestamp : null;
  const duration =
    result !== undefined && startedAt !== null && result.ctx.timestamp >= startedAt
      ? result.ctx.timestamp - startedAt
      : undefined;

  let state: ToolState;
  if (output !== null) {
    state = "output-available";
  } else if (lifecycle === "failed") {
    state = "output-error";
  } else if (lifecycle === "cancelled") {
    state = "cancelled";
  } else if (isLiveToolCall) {
    // The streaming turn is currently parked in a trailing tool phase.
    // Older result-less tools in the same turn should not inherit this
    // spinner once later text/reasoning/tool phases have appeared.
    state = "input-available";
  } else {
    // Terminal turn (completed/incomplete) with no result: never spin.
    state = "no-output";
  }

  return { kind: "tool", itemId, execution, output, state, startedAt, duration };
}

/** Same keys, each value strictly equal. */
function shallowEqual(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((k) => a[k] === b[k]);
}

/**
 * `React.memo` comparator for bubbles: true when two bubbles render
 * identically, so an unchanged bubble skips re-rendering (and re-running
 * markdown + syntax highlighting) when a streaming delta rebuilds the list.
 *
 * `buildBubbles` reuses the underlying block objects (text strings, tool
 * executions, user content arrays) across rebuilds, so primitive/reference
 * equality on each render-item field is sufficient — only the streaming
 * bubble's growing items differ, so it alone re-renders.
 */
export function bubblesEqual(a: Bubble, b: Bubble): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "assistant" && b.kind === "assistant") {
    if (a.stableId !== b.stableId || a.lifecycle !== b.lifecycle || a.error !== b.error) {
      return false;
    }
    if (a.items.length !== b.items.length) return false;
    return a.items.every((item, i) =>
      shallowEqual(
        item as unknown as Record<string, unknown>,
        b.items[i] as unknown as Record<string, unknown>,
      ),
    );
  }
  if (a.kind === "user" && b.kind === "user") {
    if (
      a.itemId !== b.itemId ||
      a.createdBy !== b.createdBy ||
      a.stableKey !== b.stableKey ||
      a.content.length !== b.content.length
    )
      return false;
    return a.content.every((block, i) => block === b.content[i]);
  }
  if (
    (a.kind === "compaction" && b.kind === "compaction") ||
    (a.kind === "compaction_loading" && b.kind === "compaction_loading")
  ) {
    return a.itemId === b.itemId;
  }
  if (a.kind === "routing_decision" && b.kind === "routing_decision") {
    // Verdict fields are immutable per item, so the id alone identifies it.
    return a.itemId === b.itemId;
  }
  return false;
}
