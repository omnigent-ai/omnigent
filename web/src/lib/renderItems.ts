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

import type {
  AnyBlock,
  MessageContentBlock,
  RoutingDecisionBlock,
  ToolExecution,
  ToolResultBlock,
} from "./blocks";
import { LIVE_ITEM_PREFIX } from "./blocks";
import {
  type RoutingDecisionExtras,
  isSessionScopedDecision,
  routingExtras,
} from "./routingDecision";
import { isSystemUserContent } from "./systemMessage";
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
      /**
       * Wall-clock seconds the turn spent working, when derivable —
       * labels the completed turn's "Worked for Xs" process fold.
       * Live-streamed turns span the blocks' page-relative timestamps;
       * reloaded history spans the items' server `created_at` stamps.
       */
      workedForS?: number;
      /**
       * Server epoch seconds of the turn's newest item, when known.
       * A just-active trace mounted from history may still be mid-turn
       * (a reload inside a step-wise turn's between-step gap), so the
       * fold holds briefly instead of appearing instantly.
       */
      lastActivityAtS?: number;
      /**
       * The turn's work continues in a LATER assistant bubble: this one
       * yielded mid-task (e.g. dispatching sub-agents and awaiting their
       * results), so the answer lands under a different response id. Lets
       * the renderer fold this bubble's trace even though it carries no
       * trailing answer of its own.
       */
      continued?: boolean;
    }
  | { kind: "compaction_loading"; itemId: string }
  | { kind: "compaction"; itemId: string }
  | {
      kind: "routing_decision";
      itemId: string;
      model: string;
      applied: boolean;
      rationale: string;
      agent?: string;
      /** Routing identity (harness, scope, decision id …); absent on legacy rows. */
      routing?: RoutingDecisionExtras;
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
 * :param lastBubbleCount: how many bubbles that region produced — 1 normally,
 *     more when a deferred routing chip widened the region back to the chip
 *     (the pair, plus any bubble rendered between them). Reuse drops exactly
 *     this many bubbles before re-walking the region.
 * :param supersededChips: block indexes the cached walk dropped as superseded
 *     create-time chips. A chip is only KNOWN superseded once the turn that
 *     repeats it arrives, which is usually after the chip's own bubble was
 *     finalized into the prefix — so the set is remembered and compared, and a
 *     disagreement over the prefix forces the one full rebuild that removes it.
 */
export interface BubbleCache {
  blocks: AnyBlock[] | null;
  activeResponse: ActiveResponse | null;
  interruptedResponseIds: readonly string[] | null;
  bubbles: Bubble[];
  lastBubbleStart: number;
  lastBubbleCount: number;
  supersededChips: ReadonlySet<number>;
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
    lastBubbleCount: 1,
    supersededChips: new Set<number>(),
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
  // Resolved over the whole transcript before any reuse decision: a create-time
  // chip and the turn chip that repeats it verbatim are one verdict however far
  // apart they landed, and whether the pair is still visible decides whether the
  // cached prefix may be reused at all.
  const superseded = supersededRoutingChips(blocks);
  if (cache === undefined) {
    return markContinuedTurns(
      walkBubbles(blocks, activeResponse, interruptedResponses, 0, [], new Map(), superseded)
        .bubbles,
      activeResponse,
    );
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
  const reuse = reusablePrefix(blocks, activeResponse, interruptedResponses, cache, superseded);
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
      superseded,
    );
    cache.blocks = blocks;
    cache.activeResponse = activeResponse;
    cache.interruptedResponseIds = interruptedResponseIds;
    cache.bubbles = markContinuedTurns(rest.bubbles, activeResponse);
    cache.lastBubbleStart = rest.lastBubbleStart;
    cache.lastBubbleCount = rest.lastBubbleCount;
    cache.supersededChips = superseded;
    return cache.bubbles;
  }

  // Cache miss (session switch, history prepend, in-place edit) — full rebuild.
  const full = walkBubbles(
    blocks,
    activeResponse,
    interruptedResponses,
    0,
    [],
    new Map(),
    superseded,
  );
  cache.blocks = blocks;
  cache.activeResponse = activeResponse;
  cache.interruptedResponseIds = interruptedResponseIds;
  cache.bubbles = markContinuedTurns(full.bubbles, activeResponse);
  cache.lastBubbleStart = full.lastBubbleStart;
  cache.lastBubbleCount = full.lastBubbleCount;
  cache.supersededChips = superseded;
  return cache.bubbles;
}

/**
 * Flag assistant bubbles whose turn continues in a later assistant
 * bubble, so the renderer can fold a trace that carries no answer of
 * its own.
 *
 * A turn that dispatches sub-agents must END to await their results;
 * the inbox wake then starts a NEW turn (new response id) that carries
 * the answer. That splits one logical turn across bubbles: the first
 * holds narration + tool calls and no answer, the second holds the
 * answer and no work. Neither half satisfies the fold's "did work AND
 * answered" rule on its own, so the process trace stayed expanded.
 *
 * Runtime `[System: …]` markers (the sub-agent-result wakes, timers)
 * are the await itself, not a new human turn, so scanning looks past
 * them; a real user message ends the turn and stops the scan.
 *
 * Only runs between turns. While one is arriving the transcript is
 * full of transient fragment bubbles — a streamed narration renders as
 * its own `live:` preview until its authoritative item replaces it,
 * and reasoning bursts group separately — so a codex turn that the
 * server records as ONE response can show as half a dozen bubbles that
 * appear and merge away on every delta. Marking those would fold and
 * unfold fragments continuously. Flags are also sticky: a bubble that
 * has folded never reopens because a later turn started streaming.
 *
 * Returns the same array when nothing changed; bubbles that flip are
 * REPLACED (never mutated) so `bubblesEqual` sees a difference and the
 * memoized bubble actually re-renders.
 */
function markContinuedTurns(bubbles: Bubble[], activeResponse: ActiveResponse | null): Bubble[] {
  if (activeResponse?.state === "streaming") return bubbles;
  let out: Bubble[] | null = null;
  for (let i = 0; i < bubbles.length; i += 1) {
    const bubble = bubbles[i]!;
    if (bubble.kind !== "assistant" || bubble.continued) continue;
    if (!hasAssistantContinuation(bubbles, i)) continue;
    if (out === null) out = bubbles.slice();
    out[i] = { ...bubble, continued: true };
  }
  return out ?? bubbles;
}

/**
 * Index of the last assistant bubble that actually renders content.
 *
 * "Last assistant" gates the fold suppression for the possibly-live
 * turn, so it must point at the turn's TRACE bubble. A parked
 * elicitation forms its own trailing assistant bubble whose card
 * ChatPage floats to the page bottom, leaving the bubble item-less (it
 * renders null) — counting that phantom as "last" handed the live
 * turn's trace to the fold.
 */
export function lastRenderableAssistantIndex(bubbles: readonly Bubble[]): number {
  for (let i = bubbles.length - 1; i >= 0; i -= 1) {
    const b = bubbles[i]!;
    if (b.kind === "assistant" && b.items.length > 0) return i;
  }
  return -1;
}

/**
 * Index of the last renderable assistant bubble ONLY IF it may still be
 * the session's live turn; -1 otherwise.
 *
 * A real user message after that bubble ends its turn for good — any
 * "running" status then belongs to the reply-in-flight for that newer
 * input, which has no bubble yet. Suppressing the settled bubble's
 * "Worked for" fold in that window pops it open until the new turn's
 * first item lands (seconds on native harnesses, whose items round-trip
 * through the vendor TUI). System `[System: ...]` markers don't end a
 * turn (see `hasAssistantContinuation`), so they don't clear the
 * suppression.
 */
export function liveCandidateAssistantIndex(bubbles: readonly Bubble[]): number {
  const idx = lastRenderableAssistantIndex(bubbles);
  for (let i = idx + 1; i < bubbles.length; i += 1) {
    const b = bubbles[i]!;
    if (b.kind === "user" && !isSystemUserContent(b.content)) return -1;
  }
  return idx;
}

/**
 * Whether a block's response id doesn't identify a real turn: `""`
 * (emitted before the turn was named) or a `live:` preview id. Such
 * blocks belong to the turn around them, never to a turn of their own.
 */
function isAnonymousRid(rid: string): boolean {
  return rid === "" || rid.startsWith(LIVE_ITEM_PREFIX);
}

/**
 * Blocks stamped a distinct response id ON PURPOSE so they render as
 * their own bubble: deny/failure sentinels aren't part of any turn's
 * work, and REQUEST-phase elicitations get a unique id precisely so
 * `isRequestElicitationBubble` can lift them out. A response-id change
 * into or out of one of these is a real bubble boundary, not a
 * same-turn continuation.
 */
function isStandaloneSentinelBlock(b: AnyBlock): boolean {
  return b.type === "policy_denied" || b.type === "error" || b.type === "elicitation";
}

/** Whether a later assistant bubble continues the turn started at `from`. */
function hasAssistantContinuation(bubbles: Bubble[], from: number): boolean {
  for (let i = from + 1; i < bubbles.length; i += 1) {
    const next = bubbles[i]!;
    if (next.kind === "assistant") return true;
    // A real user turn ends this one; system markers do not.
    if (next.kind === "user" && !isSystemUserContent(next.content)) return false;
    // Compaction / routing markers are mid-turn events — keep scanning.
  }
  return false;
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
 * :param superseded: create-time chips the current transcript drops, compared
 *     against the set the cached walk used.
 * :returns: ``{prefix, startBlock}`` when reuse is safe, else ``null``.
 */
function reusablePrefix(
  blocks: AnyBlock[],
  activeResponse: ActiveResponse | null,
  interruptedResponses: ReadonlySet<string>,
  cache: BubbleCache,
  superseded: ReadonlySet<number>,
): { prefix: Bubble[]; startBlock: number } | null {
  if (cache.blocks === null || cache.bubbles.length === 0 || cache.lastBubbleStart <= 0) {
    return null;
  }
  const startBlock = cache.lastBubbleStart;
  // The new array must be at least as long as the finalized prefix region.
  // Checked BEFORE any indexing below: a stale cache whose start points past
  // a now-shorter block array (session switch, history reload) must bail to a
  // full rebuild, not read off the end (crashes chipPendingBeforeRegion).
  if (blocks.length < startBlock) return null;
  // A chip separated from its message by skippable blocks (claude-native's
  // injected `/model` echo) sits BEFORE the re-walk region, because the echo
  // opened its own bubble in between. Rebuild in full so the arriving message
  // can still pick the chip up.
  if (chipPendingBeforeRegion(blocks, startBlock)) return null;
  // The turn chip that supersedes a create-time one usually arrives long after
  // that chip's bubble was finalized into the prefix, so the drop can only be
  // applied by re-walking it. One rebuild on the frame the verdict flips; after
  // it the sets agree again and reuse resumes.
  if (supersededPrefixChanged(superseded, cache.supersededChips, startBlock)) return null;
  // The finalized prefix region must be byte-for-byte (reference-for-
  // reference) unchanged.
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
  // Drop every bubble the re-walked region produced — normally just the last
  // one, but a user message and its deferred routing chip come as a pair.
  const dropped = Math.min(cache.lastBubbleCount, cache.bubbles.length);
  const prefix = cache.bubbles.slice(0, cache.bubbles.length - dropped);
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
 * Whether the supersede verdict changed for any chip inside the reused prefix.
 *
 * Only the prefix matters: a chip at or after `startBlock` is re-walked anyway,
 * so its verdict is applied fresh either way.
 */
function supersededPrefixChanged(
  next: ReadonlySet<number>,
  cached: ReadonlySet<number>,
  startBlock: number,
): boolean {
  for (const j of next) if (j < startBlock && !cached.has(j)) return true;
  for (const j of cached) if (j < startBlock && !next.has(j)) return true;
  return false;
}

/**
 * Whether a still-pairable session/turn chip sits just before `startBlock`.
 *
 * The chip counts as pairable only while every block after it is skippable or
 * a user message — once real assistant output follows, the chip is nobody's
 * verdict-on-a-message and the region can be reused as usual (so a long
 * streaming turn is not rebuilt frame after frame).
 */
function chipPendingBeforeRegion(blocks: AnyBlock[], startBlock: number): boolean {
  // Clamp into bounds: a stale cache can hand us a startBlock past the end of
  // a now-shorter array, and blocks[k] would be undefined (blank-page crash).
  let k = Math.min(startBlock, blocks.length) - 1;
  while (k >= 0 && isChipPairingSkippable(blocks[k]!)) k -= 1;
  if (k < 0) return false;
  const chip = blocks[k]!;
  if (chip.type !== "routing_decision" || !isSessionScopedDecision(chip.routing?.scope))
    return false;
  for (let j = k + 1; j < blocks.length; j += 1) {
    const b = blocks[j]!;
    if (!isChipPairingSkippable(b) && b.type !== "user_message") return false;
  }
  return true;
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
 * :returns: the bubble list, the block index where the last bubble began
 *     (``-1`` if no bubbles were produced at all), and how many bubbles that
 *     final region produced — counted, not assumed: a chip deferred below its
 *     user message widens the region back to the chip, and anything rendering
 *     in between (claude-native's injected `/model` echo) is inside it too.
 * :param superseded: whole-transcript block indexes of create-time chips the
 *     first turn repeats verbatim; they render nothing and claim no message.
 */
function walkBubbles(
  blocks: AnyBlock[],
  activeResponse: ActiveResponse | null,
  interruptedResponses: ReadonlySet<string>,
  startIndex: number,
  seedBubbles: Bubble[],
  subIndexByResp: Map<string, number>,
  superseded: ReadonlySet<number>,
): { bubbles: Bubble[]; lastBubbleStart: number; lastBubbleCount: number } {
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
  // A session/turn chip reads as the verdict on the message that triggered it,
  // so it renders below that message. Persistence order varies by harness:
  // native paths write the decision first, others write it after.
  const deferred = deferredRoutingChips(blocks, startIndex, superseded);
  // Bubble count at the moment the walk reached each deferred chip, so a
  // message pairing back to that chip can count every bubble the widened
  // region produced (the `/model` echo between them renders its own bubble).
  const bubbleCountAtChip = new Map<number, number>();
  // Block index where the most recently pushed bubble's region started, the
  // bubble count when that region opened, and how many bubbles it produced.
  let lastBubbleStart = bubbles.length > 0 ? 0 : -1;
  let lastBubbleCount = 1;
  let i = startIndex;

  while (i < blocks.length) {
    const b = blocks[i]!;

    // Lifecycle markers don't render — they exist for the streaming
    // reducer and the eager URL update, not the renderer.
    if (isNonRenderingBlock(b)) {
      i += 1;
      continue;
    }

    if (b.type === "user_message") {
      const chipIndex = deferred.byMessage.get(i);
      // The pair's region starts at whichever block came first, so an
      // incremental re-walk rebuilds both bubbles together — plus whatever
      // rendered in between, hence the recorded count rather than `i`'s.
      lastBubbleStart = chipIndex !== undefined ? Math.min(chipIndex, i) : i;
      const regionBubbleStart =
        chipIndex !== undefined
          ? (bubbleCountAtChip.get(chipIndex) ?? bubbles.length)
          : bubbles.length;
      bubbles.push({
        kind: "user",
        itemId: b.ctx.itemId ?? `user_${i}`,
        content: b.content,
        ...(b.ctx.createdBy !== undefined ? { createdBy: b.ctx.createdBy } : {}),
        // Carry the optimistic temp id (when promoted) so bubbleKey holds
        // steady across the optimistic→committed swap — no remount/flink.
        stableKey: b.stableKey,
      });
      if (chipIndex !== undefined) {
        bubbles.push(routingChipBubble(blocks[chipIndex] as RoutingDecisionBlock, chipIndex));
      }
      lastBubbleCount = bubbles.length - regionBubbleStart;
      i += 1;
      continue;
    }

    if (b.type === "compaction_loading") {
      lastBubbleStart = i;
      lastBubbleCount = 1;
      bubbles.push({
        kind: "compaction_loading",
        itemId: b.ctx.itemId ?? `compaction_loading_${i}`,
      });
      i += 1;
      continue;
    }

    if (b.type === "compaction") {
      // Remove the loading spinner for this compaction so the user sees
      // a single transition from spinner → checkmark.  The spinner may
      // not be the immediately preceding bubble when assistant blocks
      // (text, tool calls) were streamed during compaction.
      for (let j = bubbles.length - 1; j >= 0; j--) {
        if (bubbles[j]?.kind === "compaction_loading") {
          bubbles.splice(j, 1);
          break;
        }
      }
      lastBubbleStart = i;
      lastBubbleCount = 1;
      bubbles.push({
        kind: "compaction",
        itemId: b.ctx.itemId ?? `compaction_${i}`,
      });
      i += 1;
      continue;
    }

    if (b.type === "routing_decision") {
      // The create-time pick the first turn immediately repeats — the turn
      // chip stands for both, so this one renders nothing.
      if (superseded.has(i)) {
        i += 1;
        continue;
      }
      // Emitted below the user message that follows it, once that bubble exists.
      if (deferred.indexes.has(i)) {
        bubbleCountAtChip.set(i, bubbles.length);
        i += 1;
        continue;
      }
      // Standalone muted chip at its transcript position, never folded into an
      // adjacent assistant bubble. Sub-agent decisions stay inline where they
      // occur — they belong to the spawn, not to the user's message.
      lastBubbleStart = i;
      lastBubbleCount = 1;
      bubbles.push(routingChipBubble(b, i));
      i += 1;
      continue;
    }

    // Open an assistant bubble: ONE bubble per user turn. Collect every
    // subsequent assistant-side block up to the next user/compaction/
    // routing boundary, REGARDLESS of response id. A response-id change
    // between two assistant blocks with no user message between them is
    // a continuation of the same turn, not a new one — native harnesses
    // emit reasoning before the edge that names the turn (rid "") and
    // stream text as `live:` previews, and codex's step-wise (goal/plan)
    // turns publish a distinct response id PER STEP while the items all
    // carry the thread id. Splitting on any of those rendered one turn
    // as several fragment bubbles live (each folding into its own
    // "Worked for" row) that merged back into one on reload. The group
    // tracks the LAST real response id it saw, so lifecycle matching
    // against `activeResponse` follows the live edge. Blocks that carry
    // a DISTINCT id on purpose still open their own bubble: deny/failure
    // sentinels (`policy_denied` / `error`) are not part of the turn's
    // work, and REQUEST-phase elicitations are stamped a unique id
    // precisely so they stand alone (`isRequestElicitationBubble`).
    let groupResponseId = b.ctx.responseId;
    const groupStart = i;
    // Sentinel-headed groups never absorb a different turn's blocks, in
    // either direction (see the id-change handling below).
    const groupOpenedOnSentinel = isStandaloneSentinelBlock(b);
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
      if (isNonRenderingBlock(cur)) {
        i += 1;
        continue;
      }
      // A bare tool_result never renders standalone (it folds into its
      // call's card by callId) — don't let a backdated one split the
      // open bubble. Anonymous blocks never carry turn identity.
      const curRid = cur.ctx.responseId;
      if (cur.type !== "tool_result" && !isAnonymousRid(curRid) && curRid !== groupResponseId) {
        if (
          !isAnonymousRid(groupResponseId) &&
          (isStandaloneSentinelBlock(cur) || groupOpenedOnSentinel)
        ) {
          break;
        }
        groupResponseId = curRid;
      }
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
    // Skip `live:` preview ids for keying — the authoritative item
    // replaces the preview in place, and keying off the transient id
    // would remount the whole bubble at that swap.
    const firstItemId =
      groupBlocks.find(
        (bk) =>
          bk.type !== "tool_result" &&
          bk.ctx.itemId !== null &&
          !bk.ctx.itemId.startsWith(LIVE_ITEM_PREFIX),
      )?.ctx.itemId ?? null;
    const stableId = firstItemId ?? `${groupResponseId}:${subIndex}`;

    lastBubbleStart = groupStart;
    lastBubbleCount = 1;
    const workedForS = turnWorkedForS(groupBlocks);
    const lastActivityAtS = turnLastActivityAtS(groupBlocks);
    bubbles.push({
      kind: "assistant",
      responseId: groupResponseId,
      stableId,
      lifecycle,
      error,
      items: buildAssistantItems(groupBlocks, lifecycle, crossBubbleResults),
      ...(workedForS !== undefined ? { workedForS } : {}),
      ...(lastActivityAtS !== undefined ? { lastActivityAtS } : {}),
    });
  }

  return { bubbles, lastBubbleStart, lastBubbleCount };
}

/** Build the standalone chip bubble for a routing-decision block. */
function routingChipBubble(b: RoutingDecisionBlock, index: number): Bubble {
  return {
    kind: "routing_decision",
    itemId: b.ctx.itemId ?? `routing_${index}`,
    model: b.model,
    applied: b.applied,
    rationale: b.rationale,
    ...(b.agent !== undefined && { agent: b.agent }),
    routing: routingExtras(b.routing),
  };
}

/**
 * Pair each user message with the routing decision it triggered, for the chips
 * that need moving to land below their message.
 *
 * A session/turn decision is adjacent to the message it routes — only
 * non-content blocks may sit between (see `isChipPairingSkippable`) — but on
 * which side depends on the harness: native paths persist and stream the
 * decision BEFORE the message,
 * every other path after. Only the before-case needs deferring; a chip already
 * following its message renders below it at its own position. Adjacency keeps
 * the pair inside the incremental re-walk region, since a user bubble stays the
 * last bubble until some later block opens the next one. Sub-agent decisions,
 * and chips with no user message on either side, stay strictly in item order.
 *
 * :param startIndex: first block the walk will visit; earlier blocks belong to
 *     the reused prefix and are already placed. The neighbour lookup still
 *     reaches back across that boundary, so the pairing a region gets never
 *     depends on where the walk resumed.
 * :param superseded: chip indexes that render nothing, so they neither claim a
 *     message nor block the chip that does.
 * :returns: ``byMessage`` (user block index → chip block index) plus the set of
 *     chip indexes to skip when the walk reaches them.
 */
function deferredRoutingChips(
  blocks: AnyBlock[],
  startIndex: number,
  superseded: ReadonlySet<number>,
): { byMessage: Map<number, number>; indexes: Set<number> } {
  const byMessage = new Map<number, number>();
  const indexes = new Set<number>();
  for (let j = startIndex; j < blocks.length; j += 1) {
    const chip = blocks[j]!;
    if (chip.type !== "routing_decision" || !isSessionScopedDecision(chip.routing?.scope)) continue;
    if (superseded.has(j)) continue;
    // Already below its message — leave it where it is.
    if (adjacent(blocks, j, -1)?.type === "user_message") continue;
    const next = adjacent(blocks, j, 1);
    if (next !== null && next.type === "user_message") {
      byMessage.set(next.index, j);
      indexes.add(j);
    }
  }
  return { byMessage, indexes };
}

/**
 * Every routing chip that renders nothing because a later one stands for it.
 *
 * Two independent repeats, both resolved over the whole transcript: a
 * create-time pick the first turn repeats, and an in-harness spawn gate whose
 * resulting child session repeats it.
 *
 * :returns: block indexes of the chips that render nothing.
 */
function supersededRoutingChips(blocks: AnyBlock[]): Set<number> {
  const superseded = supersededCreateRoutingChips(blocks);
  for (const index of supersededSpawnGateRoutingChips(blocks)) superseded.add(index);
  return superseded;
}

/**
 * Spawn-gate `native_subagent` chips the resulting `child_session` chip repeats.
 *
 * One spawn produces two decisions: the in-harness gate sizes the task, and
 * then the child session it created routes its own first message. To the user
 * that is one decision about one spawn, so it renders as one chip — the
 * `child_session` row, which is strictly more informative: it names the
 * spawned agent (the gate row has no agent, so it labels itself "Session")
 * and the arm the child actually runs, with the gate's own pick still shown as
 * the row's `raw_model` when a tier substitution moved it.
 *
 * Three cases deliberately stay two chips:
 *
 * - a deny→honor pair (an unapplied gate verdict, then an applied one) — the
 *   failed attempt is the point;
 * - two different spawns, which never share both a rationale and an arm;
 * - a pair whose verdicts genuinely differ, which is not a repeat at all.
 *
 * :returns: block indexes of the gate chips that render nothing.
 */
function supersededSpawnGateRoutingChips(blocks: AnyBlock[]): Set<number> {
  const superseded = new Set<number>();
  for (let j = 0; j < blocks.length; j += 1) {
    const chip = blocks[j]!;
    if (chip.type !== "routing_decision" || chip.routing?.scope !== "native_subagent") continue;
    const later = nextRoutingDecision(blocks, j);
    if (later === null || later.routing?.scope !== "child_session") continue;
    if (sameSpawnVerdict(chip, later)) superseded.add(j);
  }
  return superseded;
}

/**
 * Whether a gate chip and the child-session chip after it decide one spawn.
 *
 * The two rows carry no shared spawn id — different decision ids, no agent name
 * on the gate row, and minutes can pass between them — so the pairing key is
 * the verdict itself: the same non-empty rationale (the router scored the same
 * task text twice) AND the child running the arm the gate picked (its
 * `raw_model`, when the workspace served a different model of that tier, else
 * its `model`). Both must hold, so two same-arm spawns of different tasks, or
 * two different verdicts about one task, still render separately.
 */
function sameSpawnVerdict(gate: RoutingDecisionBlock, child: RoutingDecisionBlock): boolean {
  if (!gate.applied || !child.applied) return false;
  if (gate.rationale.length === 0 || gate.rationale !== child.rationale) return false;
  return bareModelId(gate.model) === bareModelId(child.routing?.rawModel ?? child.model);
}

/**
 * A model id without its catalog prefix, for comparing two spellings of one arm
 * (`"databricks-claude-opus-4-8"` and the router's own `"claude-opus-4-8"`).
 */
function bareModelId(model: string): string {
  const lower = model.toLowerCase();
  return lower.startsWith("databricks-") ? lower.slice("databricks-".length) : lower;
}

/**
 * Create-time `session` chips whose verdict the first turn repeats verbatim.
 *
 * A create with Smart Routing records the pick as a `session`-scope chip, then
 * the session's first turn routes again and records the same model + harness as
 * a `turn`-scope chip. Two router calls, two audit rows — but identical chips,
 * one above the user's message and one below it. The turn chip stands for the
 * pair (it is the one that pairs below the message), so the create-time chip is
 * dropped. A create-time pick the turn then CHANGES (or a failed create-time
 * route, recorded as an unapplied `"unavailable"` row) still renders: those two
 * chips say different things.
 *
 * Scanned over the whole transcript rather than from the walk's resume point,
 * and paired by decision ORDER rather than adjacency: neither what rendered in
 * between nor which turn group the two landed in changes whether they are the
 * same verdict twice. A spawn decision (`native_subagent` / `child_session`) is
 * never the supersessor here — a deny-then-honor spawn pair is two real
 * decisions; the one spawn repeat that does collapse has its own rule in
 * `supersededSpawnGateRoutingChips`.
 *
 * :returns: block indexes of the chips that render nothing.
 */
function supersededCreateRoutingChips(blocks: AnyBlock[]): Set<number> {
  const superseded = new Set<number>();
  for (let j = 0; j < blocks.length; j += 1) {
    const chip = blocks[j]!;
    if (chip.type !== "routing_decision" || chip.routing?.scope !== "session") continue;
    const later = nextRoutingDecision(blocks, j);
    if (later === null || later.routing?.scope !== "turn") continue;
    if (sameRoutingVerdict(chip, later)) superseded.add(j);
  }
  return superseded;
}

/**
 * The next routing decision after `from`, whatever renders in between.
 *
 * Deliberately not `adjacent`: that only steps over blocks allowed to sit
 * between a chip and the message it routes, so anything a session did while
 * booting — narration, an earlier message, a whole finished response — read as
 * "these two are unrelated" and both chips rendered. Whether one verdict was
 * recorded twice is a question about their order, not their proximity, so this
 * looks past every intervening block and past turn-group boundaries.
 */
function nextRoutingDecision(blocks: AnyBlock[], from: number): RoutingDecisionBlock | null {
  for (let k = from + 1; k < blocks.length; k += 1) {
    const b = blocks[k]!;
    if (b.type === "routing_decision") return b;
  }
  return null;
}

/** Whether two routing chips advertise the same pick (model, harness, applied). */
function sameRoutingVerdict(a: RoutingDecisionBlock, b: RoutingDecisionBlock): boolean {
  return (
    a.model === b.model &&
    a.applied === b.applied &&
    (a.routing?.harness ?? null) === (b.routing?.harness ?? null) &&
    (a.agent ?? null) === (b.agent ?? null)
  );
}

/** Lifecycle markers that produce no bubble of their own. */
function isNonRenderingBlock(b: AnyBlock): boolean {
  return b.type === "response_start" || b.type === "response_end";
}

/**
 * Blocks that may sit between a routing chip and the message it routes.
 *
 * Wider than "renders nothing": `slash_command` is harness plumbing —
 * claude-native applies a routed model by typing `/model <alias>` into its TUI
 * and that injection round-trips through the transcript between the decision
 * and the user's own message — but it DOES render, as its own assistant
 * bubble, so a region spanning it produces more bubbles than the pair.
 */
function isChipPairingSkippable(b: AnyBlock): boolean {
  return isNonRenderingBlock(b) || b.type === "slash_command";
}

/** Nearest neighbour of `from` in `step` direction, skipping non-content blocks. */
function adjacent(
  blocks: AnyBlock[],
  from: number,
  step: 1 | -1,
): { type: AnyBlock["type"]; index: number } | null {
  for (let k = from + step; k >= 0 && k < blocks.length; k += step) {
    const b = blocks[k]!;
    if (isChipPairingSkippable(b)) continue;
    return { type: b.type, index: k };
  }
  return null;
}

/**
 * Server epoch seconds of the turn's newest block, when known. Lets the
 * renderer tell a JUST-active trace (a reload can land in a step-wise
 * turn's between-step gap, where everything else reads settled) from
 * genuinely old history.
 */
function turnLastActivityAtS(groupBlocks: AnyBlock[]): number | undefined {
  let latest: number | undefined;
  for (const b of groupBlocks) {
    const at = b.ctx.createdAtS;
    if (at !== undefined && (latest === undefined || at > latest)) latest = at;
  }
  return latest;
}

/**
 * Wall-clock seconds between a turn's first and last block, when both
 * ends carry a usable stamp from the SAME clock. Live-streamed blocks
 * carry page-relative `ctx.timestamp` (sub-second precision); reloaded
 * history carries server `ctx.createdAtS` (epoch seconds). A bubble
 * that mixes the two clocks (page loaded mid-turn) yields `undefined`
 * rather than a cross-clock span — the FIRST block's clock decides
 * which branch is tried: a live-stamped first block only ever pairs
 * with a live-stamped last, an epoch-stamped first only with an
 * epoch-stamped last, and either mixed direction fails both guards.
 */
function turnWorkedForS(groupBlocks: AnyBlock[]): number | undefined {
  const first = groupBlocks[0];
  const last = groupBlocks[groupBlocks.length - 1];
  if (first === undefined || last === undefined || first === last) return undefined;
  if (first.ctx.timestamp > 0 && last.ctx.timestamp >= first.ctx.timestamp) {
    return last.ctx.timestamp - first.ctx.timestamp;
  }
  const firstCreated = first.ctx.createdAtS;
  const lastCreated = last.ctx.createdAtS;
  if (firstCreated !== undefined && lastCreated !== undefined && lastCreated >= firstCreated) {
    return lastCreated - firstCreated;
  }
  return undefined;
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
    if (
      a.stableId !== b.stableId ||
      a.lifecycle !== b.lifecycle ||
      a.error !== b.error ||
      // Flips when a later bubble continues this turn — the fold depends on it.
      Boolean(a.continued) !== Boolean(b.continued)
    ) {
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
