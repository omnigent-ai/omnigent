// Dispatch from a `RenderItem` to the right component. Pure switch on
// `item.kind`. Compaction renders as a standalone `Bubble` in
// `ChatPage`, not as an inline render item — no case for it here.
//
// Two levels of collapsing keep a turn readable:
//
// 1. Tool-run folding: within a contiguous run of tool / native_tool
//    items, tools fold into a single summary line describing what's
//    hidden ("Read 2 files", rendered by `ToolGroupSummary`). While
//    the run is the live activity, the trailing `STREAMING_TAIL`
//    tools stay visible as individual rows so the user can watch
//    recent steps. Still-in-progress spinners and durable
//    routing/fan-out cards never fold regardless of position.
//
// 2. Turn folding: once the turn settles (`turnLifecycle` leaves
//    "streaming"), the whole process trace — interstitial narration,
//    tool folds, reasoning — collapses behind one muted "Worked for
//    Xs" row (`TurnWorkedFold`), leaving only the trailing final
//    answer visible. This mirrors the Codex desktop treatment: the
//    demarcation makes "where do I start reading" obvious instead of
//    a wall of uniform prose. Resolved approval cards fold with the
//    trace in document order; pending elicitations and persistent
//    routing/dispatch cards stay visible outside the fold; a turn
//    with no trailing answer (interrupted / failed / tool-only step
//    bubbles) keeps its trace expanded.

import type { ReactNode } from "react";
import { useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import { LIVE_ITEM_PREFIX } from "@/lib/blocks";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ConversationScrollLockContext } from "@/components/ai-elements/conversation";
import type { RenderItem } from "@/lib/renderItems";
import type { SessionStatus } from "@/lib/types";
import type { ActiveResponse } from "@/store/types";
import { cn } from "@/lib/utils";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";
import { ElicitationCard } from "./ApprovalCard";
import { ReasoningView } from "./ReasoningView";
import { SlashCommandCard } from "./SlashCommandCard";
import { SmartRoutingCard } from "./SmartRoutingCard";
import { TerminalCommandCard } from "./TerminalCommandCard";
import { ErrorBanner, PolicyDeniedBanner, RetryIndicator } from "./StatusBlocks";
import { ToolCard, ToolGroupSummary } from "./ToolCard";

// Re-exported for the existing import sites; it lives in ./ChatMarkdown so
// surfaces rendered *by* this module can use it without an import cycle.
export { FilePathAwareMessageResponse } from "./ChatMarkdown";

const STREAMING_TAIL = 3;

// How long fold eligibility must hold before the "Worked for" fold
// appears on a live bubble. Long enough to absorb transient settled
// reads (a step-wise turn's between-step idle, a stray idle before its
// revive), short enough to feel immediate at a real turn end.
const FOLD_SETTLE_DEBOUNCE_MS = 500;

// A trace whose newest item is at most this old counts as "just
// active": a page load over it may have landed inside a step-wise
// turn's between-step gap, where the snapshot reads settled although
// the turn continues. The last bubble's fold then waits
// `FOLD_RECENT_MOUNT_DEBOUNCE_MS` instead of appearing instantly, so
// the next step's running edge can cancel it — no fold flash. Genuinely
// old history still mounts folded with no delay.
const RECENT_ACTIVITY_WINDOW_S = 15;
const FOLD_RECENT_MOUNT_DEBOUNCE_MS = 3_000;

// How long a user-initiated fold expand parks the scroller's scroll
// anchoring. Covers the 200ms `turn-fold-expand` height animation (see
// index.css) with slack — anchoring would otherwise pin the answer
// below the fold and glide the growing trace off the top.
const FOLD_EXPAND_ANCHOR_HOLD_MS = 400;

interface BlockRendererProps {
  items: RenderItem[];
  sessionStatus: SessionStatus;
  onRetryError?: (item: Extract<RenderItem, { kind: "error" }>) => Promise<void>;
  /**
   * Lifecycle of the turn this bubble renders (`Bubble.lifecycle`).
   * `"streaming"` keeps the process trace expanded; any settled state
   * folds it behind the "Worked for Xs" row. When omitted, liveness
   * falls back to `sessionStatus` (running/waiting ⇒ live).
   */
  turnLifecycle?: ActiveResponse["state"];
  /** Wall-clock seconds the turn worked (`Bubble.workedForS`). */
  workedForS?: number;
  /**
   * The turn continues in a later assistant bubble (`Bubble.continued`)
   * — it yielded mid-task (e.g. awaiting sub-agents), so the answer
   * lands elsewhere. Such a bubble folds its whole trace despite having
   * no trailing answer of its own.
   */
  continued?: boolean;
  /**
   * This bubble is the LAST assistant bubble in the transcript. While
   * the session is running, the last bubble never folds even when its
   * lifecycle reads settled: on a mid-turn (re)connect the client can
   * miss the edge that names the turn, misreading the live turn as
   * "completed" — folding it then made the trace collapse and reopen
   * as its tail alternated between text and tools (the codex flicker).
   * The fold forms when the session's own terminal status edge lands,
   * which is the natural moment anyway.
   */
  isLastAssistant?: boolean;
  /**
   * The session has a pending elicitation (approval/question card
   * awaiting the user). The turn is parked, not over — a reload while
   * parked can read BOTH the lifecycle and the session status as
   * settled (a step-wise turn's snapshot names the STEP id, not the
   * items' thread id) — so the last bubble's fold stays suppressed
   * until the card is answered.
   */
  hasPendingElicitation?: boolean;
  /** Server epoch seconds of the turn's newest item (`Bubble.lastActivityAtS`). */
  lastActivityAtS?: number;
  /** Whether this final bubble is still part of a visible active turn. */
  showsWorking?: boolean;
}

/** The subset of {@link BlockRendererProps} the fold decision reads. */
type FoldInputs = Pick<
  BlockRendererProps,
  | "items"
  | "sessionStatus"
  | "turnLifecycle"
  | "continued"
  | "isLastAssistant"
  | "hasPendingElicitation"
  | "showsWorking"
>;

/**
 * How live a turn is, from the bubble's own lifecycle and the session's.
 *
 * `isOwnTurnLive` — streaming into THIS bubble; only this clears the
 * shown-fold latch. `possiblyLive` — the last assistant bubble while the
 * session works or parks on an elicitation MAY be the live turn even when
 * its lifecycle reads settled (a mid-turn (re)connect can miss the edge
 * that names the turn).
 */
function turnLiveness({
  sessionStatus,
  turnLifecycle,
  isLastAssistant = false,
  hasPendingElicitation = false,
  showsWorking = false,
}: Omit<FoldInputs, "items" | "continued">): {
  isOwnTurnLive: boolean;
  possiblyLive: boolean;
  isTurnLive: boolean;
} {
  const isAgentActive = sessionStatus === "running" || sessionStatus === "waiting";
  const isOwnTurnLive = turnLifecycle !== undefined ? turnLifecycle === "streaming" : isAgentActive;
  const possiblyLive =
    isLastAssistant && (showsWorking || sessionStatus === "running" || hasPendingElicitation);
  return { isOwnTurnLive, possiblyLive, isTurnLive: isOwnTurnLive || possiblyLive };
}

/**
 * Whether the turn's CONTENT can fold: it did work, and either answered
 * here or continues in a later bubble. A turn that did no work, or that
 * dead-ends with no answer anywhere, renders expanded — there is nothing
 * to demarcate.
 *
 * A `continued` bubble additionally has to have RUN something. That is
 * the shape the flag exists for (narration + tool calls, then a yield to
 * await sub-agents), and it keeps a stray narration- or reasoning-only
 * fragment of a split turn from folding into a lone "Worked" row with
 * nothing behind it.
 *
 * Liveness is the caller's half of the decision — `BlockRenderer` pairs
 * this with its fold latch.
 */
function hasFoldableShape(
  items: RenderItem[],
  { process, final }: TurnPartition,
  continued = false,
): boolean {
  return (
    !isProvisionalTrace(items) &&
    process.length > 0 &&
    (final.length > 0 || (continued && process.some(isToolItem)))
  );
}

/**
 * Whether the bubble renders NOTHING but the collapsed "Worked for" row:
 * a turn fragment that yielded mid-task, so its whole trace folds and the
 * answer lands in a later bubble.
 *
 * Such a bubble has no visible content to anchor bubble-level chrome to.
 * The copy/fork actions key off ALL the bubble's text, including text
 * sealed inside the fold, so they would hang a row of hover-only (hence
 * invisible) height off it — but only when the HIDDEN trace happened to
 * narrate, leaving consecutive collapsed rows at two different gaps with
 * nothing on screen to explain the difference.
 *
 * Deliberately conservative about the possibly-live last bubble: this
 * can't see `BlockRenderer`'s fold latch, so it reports false there and
 * the trailing bubble keeps its actions. True here always means the trace
 * folded — never the reverse, which would strip actions off a visible
 * answer.
 *
 * Shape and liveness only — no debounce. Across `BlockRenderer`'s settle
 * window the two answers may differ for a beat; on a bubble with no
 * answer to anchor them to, that costs nothing visible.
 */
export function rendersOnlyWorkedFold(inputs: FoldInputs): boolean {
  const { isOwnTurnLive, possiblyLive } = turnLiveness(inputs);
  if (isOwnTurnLive || possiblyLive) return false;
  const partition = partitionTurn(inputs.items);
  return (
    hasFoldableShape(inputs.items, partition, inputs.continued) && partition.final.length === 0
  );
}

type ToolRunFragment =
  | {
      kind: "group";
      tools: RenderItem[];
    }
  | {
      kind: "standalone";
      tool: RenderItem;
      index: number;
    };

export function BlockRenderer({
  items,
  sessionStatus,
  turnLifecycle,
  workedForS,
  continued = false,
  isLastAssistant = false,
  hasPendingElicitation = false,
  lastActivityAtS,
  showsWorking = false,
  onRetryError,
}: BlockRendererProps) {
  const { isOwnTurnLive, possiblyLive, isTurnLive } = turnLiveness({
    sessionStatus,
    turnLifecycle,
    isLastAssistant,
    hasPendingElicitation,
    showsWorking,
  });

  // Fold a turn that did work AND either answered here or continues in a
  // later bubble: the trace collapses behind the "Worked for" row, exempt
  // cards stay visible after it, and the answer (when this bubble carries
  // one) renders last at full style. A turn that did no work, or that
  // dead-ends with no answer anywhere, renders expanded — there is
  // nothing to demarcate.
  const partition = partitionTurn(items);
  const { process, exempt, final, finalStart } = partition;
  // Once the fold has SHOWN on a settled bubble, possibly-live no
  // longer reopens it. A scheduled wake (a /loop cron or wakeup
  // firing) flips the session to running — Working shimmer included —
  // while the settled bubble is still the last one and the new turn
  // has no items yet; without the latch that item-less gap popped the
  // fold open every iteration. Only this bubble's OWN turn going live
  // again (a revive) clears the latch and re-expands the trace.
  const foldLatchedRef = useRef(false);
  const foldEligible =
    !isOwnTurnLive &&
    // Never fold a possibly-live bubble whose fold hasn't shown yet:
    // the terminal status edge is what folds it (see possiblyLive).
    (!possiblyLive || foldLatchedRef.current) &&
    hasFoldableShape(items, partition, continued);

  // Debounce fold APPEARANCE on a live bubble: transient settled reads —
  // a step-wise turn's idle edge between steps, a stray bare idle before
  // its revive — would otherwise fold and unfold the trace. Eligibility
  // must hold for a beat before the fold shows; losing eligibility hides
  // it immediately. A bubble MOUNTED eligible (settled history) folds
  // with no delay — UNLESS it is the last bubble over a just-active
  // trace, where the page may have loaded inside a step-wise turn's
  // between-step gap: that mount waits the longer recent-mount debounce
  // so the next step's running edge can cancel the fold instead of
  // flashing it.
  const mountedOverRecentActivity =
    isLastAssistant &&
    lastActivityAtS !== undefined &&
    Date.now() / 1000 - lastActivityAtS < RECENT_ACTIVITY_WINDOW_S;
  const [showFold, setShowFold] = useState(foldEligible && !mountedOverRecentActivity);
  const debounceMsRef = useRef(
    mountedOverRecentActivity ? FOLD_RECENT_MOUNT_DEBOUNCE_MS : FOLD_SETTLE_DEBOUNCE_MS,
  );
  useEffect(() => {
    if (foldEligible === showFold) {
      // Once the fold has shown (or the mount window resolved), later
      // transitions use the ordinary settle debounce.
      if (showFold) debounceMsRef.current = FOLD_SETTLE_DEBOUNCE_MS;
      return undefined;
    }
    if (!foldEligible) {
      debounceMsRef.current = FOLD_SETTLE_DEBOUNCE_MS;
      setShowFold(false);
      return undefined;
    }
    const timer = setTimeout(() => setShowFold(true), debounceMsRef.current);
    return () => clearTimeout(timer);
  }, [foldEligible, showFold]);

  // Animate only when the fold APPEARS on an already-mounted bubble —
  // the turn settled, or its continuation landed, while the user was
  // looking. Mounting settled history shows it closed with no motion.
  const mountedRef = useRef(false);
  const foldShownRef = useRef(showFold);
  const animateCollapse = showFold && mountedRef.current && !foldShownRef.current;
  useEffect(() => {
    mountedRef.current = true;
    foldShownRef.current = showFold;
    if (isOwnTurnLive) foldLatchedRef.current = false;
    else if (showFold) foldLatchedRef.current = true;
  });

  if (showFold) {
    return (
      <>
        <TurnWorkedFold workedForS={workedForS} animateCollapse={animateCollapse}>
          {renderSequence(process, { liveEdge: false })}
        </TurnWorkedFold>
        {exempt.map(({ item, index }) =>
          renderItem(item, index, false, false, false, onRetryError),
        )}
        {renderSequence(final, { liveEdge: false, indexBase: finalStart, onRetryError })}
      </>
    );
  }

  return renderSequence(items, {
    liveEdge: isTurnLive,
    suppressReasoningDuration: showsWorking,
    onRetryError,
  });
}

/**
 * Render a flat item sequence with tool-run folding. `liveEdge` marks
 * the sequence as the live activity: the trailing tool run keeps its
 * visible `STREAMING_TAIL` and a trailing reasoning item shows the
 * streaming shimmer. `indexBase` offsets positional fallback keys so a
 * partitioned slice keys consistently with its position in the turn.
 */
function renderSequence(
  items: RenderItem[],
  { liveEdge, suppressReasoningDuration = false, indexBase = 0, onRetryError }: TurnSequenceOptions,
): ReactNode[] {
  const rendered: ReactNode[] = [];
  let previousRenderedItemWasText = false;
  const streamingRunStart = liveEdge ? findStreamingRunStart(items) : -1;
  // Reasoning is "currently streaming" iff the turn is live AND this
  // reasoning is the very last item in the bubble. Mirrors the
  // `streamingRunStart` rule for tool runs: the trailing live edge stays
  // expanded; once anything else lands after it, it collapses.
  const lastIdx = items.length - 1;
  const reasoningStreamingIdx =
    liveEdge && lastIdx >= 0 && items[lastIdx]!.kind === "reasoning" ? lastIdx : -1;

  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]!;

    if (isToolItem(item)) {
      // Consume contiguous run of tool / native_tool items.
      const runStart = i;
      while (i < items.length && isToolItem(items[i]!)) i += 1;
      const run = items.slice(runStart, i);
      i -= 1; // outer loop will i += 1

      // Only the run at `streamingRunStart` (when set) keeps a visible
      // tail. Earlier runs, and any run followed by assistant
      // text/reasoning, fold entirely the same way they would when idle.
      const isStreamingRun = runStart === streamingRunStart;
      const fragments = partitionToolRun(run, isStreamingRun);

      if (isStreamingRun && fragments[0]?.kind === "group") {
        const [group, ...tail] = fragments;
        // Wrap (fold line + trailing tail) in a single MessageContent
        // child so the message column's `gap-2` only applies AROUND the
        // run, not BETWEEN its rows. The tail rows sit at the same
        // indent as the fold line — they're peers in one flat run, like
        // the terminal's step list; only the fold's expanded contents
        // are nested (inside `ToolGroupSummary`).
        rendered.push(
          <div key={`tool-group-with-tail:${indexBase + runStart}`} className="space-y-1">
            <ToolGroupSummary tools={group.tools} />
            {tail.map((fragment, idx) =>
              renderToolRunFragment(fragment, indexBase + runStart, idx),
            )}
          </div>,
        );
      } else {
        for (let idx = 0; idx < fragments.length; idx += 1) {
          rendered.push(renderToolRunFragment(fragments[idx]!, indexBase + runStart, idx));
        }
      }
      previousRenderedItemWasText = false;
      continue;
    }

    const followsText = item.kind === "text" && previousRenderedItemWasText;
    rendered.push(
      renderItem(
        item,
        indexBase + i,
        i === reasoningStreamingIdx,
        suppressReasoningDuration,
        followsText,
        onRetryError,
      ),
    );
    previousRenderedItemWasText = item.kind === "text";
  }

  return rendered;
}

interface TurnSequenceOptions {
  liveEdge: boolean;
  suppressReasoningDuration?: boolean;
  indexBase?: number;
  onRetryError?: BlockRendererProps["onRetryError"];
}

interface TurnPartition {
  process: RenderItem[];
  exempt: { item: RenderItem; index: number }[];
  final: RenderItem[];
  finalStart: number;
}

// Bookkeeping tools some harnesses append AFTER the turn's final
// message (codex-native mirrors the turn's file diff as a trailing
// `turn_diff` call). They must not stop the answer detection — they
// fold into the process trace instead.
const TRAILING_WRAPUP_TOOLS = new Set(["turn_diff"]);

/**
 * Whether a trailing item is wrap-up rather than part of the answer.
 *
 * Reasoning counts: it is process by definition, never the answer, and
 * codex opens a reasoning section as the turn ends — landing it after
 * the final message, where it blocked the fold live (the item is
 * transient, so a reload folded the same turn and the two views
 * disagreed).
 */
function isTrailingWrapup(item: RenderItem): boolean {
  if (item.kind === "reasoning") return true;
  return item.kind === "tool" && TRAILING_WRAPUP_TOOLS.has(item.execution.name);
}

/**
 * Split a settled turn into the foldable process trace, the always-
 * visible exempt items, and the trailing final answer.
 *
 * `final` is the trailing run of text items — the turn's answer —
 * looking past any trailing bookkeeping tools (`turn_diff`), which
 * fold as process. Everything before the answer is process too —
 * including resolved approval cards, which are part of the work's
 * history and fold in document order — except items the user must
 * keep seeing without an extra click: still-PENDING elicitations
 * (normally floated out of the bubble by ChatPage, exempted here
 * defensively so an actionable card can never be hidden), persistent
 * routing/dispatch cards, and (defensively) tools still in progress.
 * Errors, retries and policy denials DO fold — when the turn still
 * produced an answer they're recovered noise, and a turn that ended
 * on one has no trailing text so it never folds in the first place.
 */
function partitionTurn(items: RenderItem[]): TurnPartition {
  let end = items.length;
  const wrapup: RenderItem[] = [];
  while (end > 0 && isTrailingWrapup(items[end - 1]!)) {
    wrapup.unshift(items[end - 1]!);
    end -= 1;
  }
  let finalStart = end;
  while (finalStart > 0 && items[finalStart - 1]!.kind === "text") finalStart -= 1;
  const process: RenderItem[] = [];
  const exempt: { item: RenderItem; index: number }[] = [];
  for (let i = 0; i < finalStart; i += 1) {
    const item = items[i]!;
    if (isPendingElicitation(item) || isPersistentToolCard(item) || isInProgressTool(item)) {
      exempt.push({ item, index: i });
    } else {
      process.push(item);
    }
  }
  process.push(...wrapup);
  return { process, exempt, final: items.slice(finalStart, end), finalStart };
}

function isPendingElicitation(item: RenderItem): boolean {
  return item.kind === "elicitation" && item.status === "pending";
}

/**
 * Whether a bubble is made ONLY of streaming artifacts: reasoning
 * chunks (no item id until their item is finalized) and `live:` text
 * previews (replaced in place when the authoritative item lands). Such
 * a bubble is a fragment of the turn still arriving, not a finished
 * turn — but its synthetic response id never matches `activeResponse`,
 * so the walker labels it "completed" and it would otherwise fold.
 *
 * That is what made the fold flicker on codex: a reasoning burst plus a
 * narration preview folded mid-turn, then the fold vanished when the
 * preview merged into the real bubble. A genuine turn always carries at
 * least one server-assigned item id.
 */
function isProvisionalTrace(items: RenderItem[]): boolean {
  return items.every((item) => item.itemId === null || item.itemId.startsWith(LIVE_ITEM_PREFIX));
}

/**
 * Codex-style demarcation for a completed turn: the whole process
 * trace (narration, tool folds, reasoning) collapses behind one muted
 * "Worked for Xs" disclosure, so the final answer below is
 * unambiguously where reading starts. Expanding replays the trace
 * beside a compact vertical guide.
 *
 * `animateCollapse` marks the render where the fold appeared while the
 * user was watching. The fold then MOUNTS OPEN — showing exactly the
 * trace that was already on screen — and closes on the next frame, so
 * the steps visibly fold into the summary row. Swapping straight to the
 * collapsed row instead made a tall block vanish in one frame, which
 * read as a partial page reload. Settled history mounts closed: there
 * was never an expanded trace to animate away.
 *
 * The summary row grows in over the same beat rather than being
 * inserted at full height, because inserting it jolted the bubble ~40px
 * TALLER before the collapse started — read as two separate motions.
 * Row expanding + trace shrinking nets a single monotonic shrink.
 */
function TurnWorkedFold({
  workedForS,
  animateCollapse,
  children,
}: {
  workedForS?: number;
  animateCollapse: boolean;
  children: ReactNode;
}) {
  const label = workedForS !== undefined ? `Worked for ${formatWorkedFor(workedForS)}` : "Worked";
  const [open, setOpen] = useState(animateCollapse);
  useEffect(() => {
    if (!animateCollapse) return;
    const frame = requestAnimationFrame(() => setOpen(false));
    return () => cancelAnimationFrame(frame);
  }, [animateCollapse]);

  // A USER-initiated expand (never the animateCollapse mount-close)
  // opens INSTANTLY — no height animation — and snaps the fold row to
  // the top of the scroller so the trace reads from its beginning. The
  // animation is what defeated the snap: it opens at height 0, so at
  // snap time the scroller has no room yet (the snap clamps at the old
  // max-scroll), and the 200ms of growth then plays out against the
  // stick-to-bottom pin (which rides the bottom on the last turn) or
  // native scroll anchoring (which pins the answer below) — either way
  // the row glides off the top and the click appears to do nothing.
  // Instant height means the first layout already has the full trace:
  // the snap lands, and there is no growth window left to fight over.
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [userOpened, setUserOpened] = useState(false);
  const scrollOnOpenRef = useRef(false);
  const scrollLock = useContext(ConversationScrollLockContext);
  const handleOpenChange = (next: boolean) => {
    scrollOnOpenRef.current = next;
    setUserOpened(next);
    setOpen(next);
  };
  useLayoutEffect(() => {
    if (!open || !scrollOnOpenRef.current) return;
    scrollOnOpenRef.current = false;
    const row = rowRef.current;
    if (!row) return;
    // Release StickToBottom's bottom-lock first (same recipe as
    // JumpToTopButton): viewing the last turn the view is pinned, and
    // the expand's resize otherwise fires the library's scrollToBottom
    // (isAtBottom is still true from the pre-click view), overriding
    // the snap and riding the bottom — the click appears to do nothing.
    if (scrollLock) {
      scrollLock.stopScroll();
      scrollLock.state.isAtBottom = false;
      scrollLock.state.escapedFromLock = true;
    }
    const scroller = nearestScrollContainer(row);
    // Park native scroll anchoring across the expand commit. The
    // restore deliberately outlives the effect (no cleanup): the timer
    // must fire even if the fold re-renders or unmounts mid-hold.
    if (scroller) {
      scroller.style.overflowAnchor = "none";
      window.setTimeout(() => {
        scroller.style.overflowAnchor = "";
      }, FOLD_EXPAND_ANCHOR_HOLD_MS);
    }
    row.scrollIntoView?.({ block: "start" });
  }, [open, scrollLock]);

  return (
    // Named `group/turn-fold` so only this collapsible's own chevron
    // rotates (inner tool cards carry unnamed `.group` rotations that a
    // bare `group` class here would incorrectly trigger).
    <Collapsible
      key="turn-worked-fold"
      open={open}
      onOpenChange={handleOpenChange}
      className="group/turn-fold not-prose w-full"
      data-testid="turn-worked-fold"
    >
      <div ref={rowRef} className={cn("turn-fold-row", animateCollapse && "turn-fold-row-enter")}>
        <CollapsibleTrigger className="flex cursor-pointer items-center gap-2 rounded-sm py-1 text-left text-muted-foreground text-chat outline-none transition-colors hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          <span className="shrink-0">{label}</span>
          <ChevronRightIcon className="size-2.5 shrink-0 transition-transform group-data-[state=open]/turn-fold:rotate-90" />
        </CollapsibleTrigger>
      </div>
      {/* Height animation lives in index.css (it needs Radix's measured
          --radix-collapsible-content-height) and is disabled under
          prefers-reduced-motion. */}
      {/* Keep pt-2/pl-4 and the vertical pin inside the collapsible
          content so the spacing and guide shrink with its existing
          height animation. */}
      <CollapsibleContent
        className={cn("turn-fold-content", userOpened && "turn-fold-content-instant")}
      >
        <div className="relative flex flex-col gap-1 pt-2 pl-4">
          <span
            aria-hidden
            className="absolute top-2 bottom-0 left-1 w-px bg-border"
            data-testid="turn-worked-fold-pin-line"
          />
          {children}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

/** Nearest ancestor that actually scrolls vertically (overflow-y auto/scroll). */
function nearestScrollContainer(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const overflowY = getComputedStyle(p).overflowY;
    if (overflowY === "auto" || overflowY === "scroll") return p;
  }
  return null;
}

/** "8s", "1m 46s", "1h 2m" — matches the native CLIs' worked-for stamps. */
function formatWorkedFor(seconds: number): string {
  const s = Math.max(1, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm > 0 ? `${h}h ${mm}m` : `${h}h`;
}

/**
 * Split a contiguous tool run into the part that folds into the
 * collapsed summary group versus the part rendered individually.
 *
 * For the live-streaming run, the trailing `STREAMING_TAIL` tools
 * (regardless of state) stay outside the group so the user can watch
 * the most recent activity; any other run folds entirely. In-progress
 * spinners and persistent routing plan cards never fold, so a routing
 * judgement isn't swallowed mid-fan-out when later spawns push it past
 * the tail window.
 */
function partitionToolRun(run: RenderItem[], isStreamingRun: boolean): ToolRunFragment[] {
  const tailStart = isStreamingRun ? Math.max(0, run.length - STREAMING_TAIL) : run.length;
  const fragments: ToolRunFragment[] = [];
  let group: RenderItem[] = [];
  const flushGroup = () => {
    if (group.length === 0) return;
    fragments.push({ kind: "group", tools: group });
    group = [];
  };
  for (let index = 0; index < run.length; index += 1) {
    const item = run[index]!;
    if (index >= tailStart || isInProgressTool(item) || isPersistentToolCard(item)) {
      flushGroup();
      fragments.push({ kind: "standalone", tool: item, index });
    } else {
      group.push(item);
    }
  }
  flushGroup();
  return fragments;
}

function renderToolRunFragment(
  fragment: ToolRunFragment,
  runStart: number,
  fragmentIndex: number,
): ReactNode {
  if (fragment.kind === "group") {
    return (
      <ToolGroupSummary key={`tool-group:${runStart}:${fragmentIndex}`} tools={fragment.tools} />
    );
  }
  return renderItem(fragment.tool, runStart + fragment.index, false);
}

const ADVISE_MODELS_NAMES = new Set(["sys_advise_models", "mcp__omnigent__sys_advise_models"]);
const SESSION_SEND_NAMES = new Set(["sys_session_send", "mcp__omnigent__sys_session_send"]);

function isPersistentToolCard(item: RenderItem): boolean {
  return (
    item.kind === "tool" &&
    (ADVISE_MODELS_NAMES.has(item.execution.name) || SESSION_SEND_NAMES.has(item.execution.name))
  );
}

function isToolItem(item: RenderItem): boolean {
  return item.kind === "tool" || item.kind === "native_tool";
}

/**
 * If the transcript ends in a contiguous tool run, return its start
 * index — that run is the live activity and should keep its
 * streaming tail. Otherwise return -1: the agent has spoken (or
 * reasoned) after the most recent tools, so they're no longer
 * "current".
 */
function findStreamingRunStart(items: RenderItem[]): number {
  if (items.length === 0) return -1;
  if (!isToolItem(items[items.length - 1]!)) return -1;
  let i = items.length - 1;
  while (i > 0 && isToolItem(items[i - 1]!)) i -= 1;
  return i;
}

/**
 * A tool item is in-progress only when it's a `tool` (not a
 * `native_tool` — those are provider-managed and always arrive
 * completed) and its derived UI state is `input-available`.
 */
function isInProgressTool(item: RenderItem): boolean {
  return item.kind === "tool" && item.state === "input-available";
}

function renderItem(
  item: RenderItem,
  index: number,
  isReasoningStreaming: boolean,
  suppressReasoningDuration = false,
  followsText = false,
  onRetryError?: BlockRendererProps["onRetryError"],
): ReactNode {
  const key = keyFor(item, index);
  switch (item.kind) {
    case "text":
      return (
        <div
          key={key}
          data-testid="assistant-text-section"
          className={cn("min-w-0", followsText && "mt-2")}
        >
          <FilePathAwareMessageResponse>{item.text}</FilePathAwareMessageResponse>
        </div>
      );
    case "reasoning":
      return (
        <ReasoningView
          key={key}
          text={item.text}
          isStreaming={isReasoningStreaming}
          duration={suppressReasoningDuration ? undefined : item.duration}
        />
      );
    case "tool":
      // Intelligent routing's fan-out sizing gets a structured plan card
      // instead of the generic name(json) row + raw-JSON expansion.
      if (ADVISE_MODELS_NAMES.has(item.execution.name)) {
        return (
          <SmartRoutingCard
            key={key}
            arguments={item.execution.arguments}
            output={item.output}
            state={item.state}
          />
        );
      }
      return (
        <ToolCard
          key={key}
          name={item.execution.name}
          argsSummary={item.execution.argsSummary}
          arguments={item.execution.arguments}
          output={item.output}
          state={item.state}
          startedAt={item.startedAt}
          duration={item.duration}
        />
      );
    case "native_tool":
      // Reuse the same tool card. Native tools are server-side
      // (provider-managed) so they're always "completed" by the
      // time we see them; render the raw provider data as input.
      return (
        <ToolCard
          key={key}
          name={item.label}
          nativeToolType={item.toolType}
          arguments={item.data}
          output={null}
          state="output-available"
        />
      );
    case "slash_command":
      return (
        <SlashCommandCard
          key={key}
          kind={item.slashKind}
          name={item.name}
          arguments={item.arguments}
          output={item.output}
        />
      );
    case "terminal_command":
      return (
        <TerminalCommandCard
          key={key}
          kind={item.terminalKind}
          input={item.input}
          stdout={item.stdout}
          stderr={item.stderr}
        />
      );
    case "error":
      return (
        <ErrorBanner
          key={key}
          message={item.message}
          source={item.source}
          code={item.code}
          title={item.title}
          cause={item.cause}
          remediation={item.remediation}
          level={item.level}
          onRetry={onRetryError ? () => onRetryError(item) : undefined}
        />
      );
    case "policy_denied":
      return <PolicyDeniedBanner key={key} reason={item.reason} phase={item.phase} />;
    case "retry":
      return (
        <RetryIndicator
          key={key}
          source={item.source}
          attempt={item.attempt}
          maxAttempts={item.maxAttempts}
          delaySeconds={item.delaySeconds}
        />
      );
    case "elicitation":
      return <ElicitationCard key={key} item={item} />;
  }
}

/**
 * Stable key for each render item. Prefer the server-assigned item id;
 * fall back to call_id for tools (unique within a response) or to
 * position for pre-finalization fragments that don't carry an item id
 * yet (text/reasoning chunks emitted before their `output_item.done`).
 */
function keyFor(item: RenderItem, index: number): string {
  if (item.itemId) return `${item.kind}:${item.itemId}`;
  if (item.kind === "tool") return `tool:${item.execution.callId}`;
  if (item.kind === "elicitation") return `elicitation:${item.elicitationId}`;
  return `${item.kind}:${index}`;
}
