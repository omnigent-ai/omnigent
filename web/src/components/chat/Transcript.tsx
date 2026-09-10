import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useStickToBottomContext } from "use-stick-to-bottom";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { Message, MessageContent } from "@/components/ai-elements/message";
import { ElicitationCard } from "@/components/blocks/ApprovalCard";
import { cn } from "@/lib/utils";
import { getCurrentAuthorId } from "@/lib/identity";
import { hasCommandModifier } from "@/lib/hotkeys";
import { isSystemUserContent } from "@/lib/systemMessage";
import {
  type Bubble,
  type BubbleCache,
  buildBubbles,
  createBubbleCache,
  liveCandidateAssistantIndex,
} from "@/lib/renderItems";
import { useChatStore } from "@/store/chatStore";
import { TranscriptScrollbar } from "@/pages/TranscriptScrollbar";
import { TurnRail, type Turn } from "@/pages/TurnRail";
import { StreamBudgetBanner } from "@/components/StreamBudgetBanner";
import { useUserMessageNav } from "@/hooks/useUserMessageNav";
import { ChatPlanAccordion } from "@/shell/ChatPlanAccordion";
import {
  RunnerStartingIndicator,
  McpStartupIndicator,
  CodexStartupPromptIndicator,
} from "@/pages/ChatIndicators";
import { CHAT_COLUMN_WIDTH } from "@/pages/chatLayout";
import {
  type ConversationScroller,
  BubbleView,
  ConversationScrollRefBridge,
  HistoryAutoLoader,
  HistoryLoadingIndicator,
  JumpToTopButton,
  KeepBottomOnViewportResize,
  LatestTurnSpacer,
  ScrollToBottomOnSend,
  UserMessageNavConnected,
  WorkingIndicator,
  bubbleKey,
  buildPendingBubbles,
  collectPendingElicitations,
  computeIsWorking,
  extractUserText,
  isSystemBubble,
  mergePendingBubbles,
  reorderCommittedRequestElicitations,
  shouldShowWorkingIndicator,
  stripGatedSubagentRoutingChips,
  stripPendingElicitations,
} from "@/components/chat/chatBubbleParts";
import { SCROLL_RESTORE_BUDGET_MS } from "@/shell/useScrollRestore";

export interface TranscriptProps {
  /** Ref callback for the conversation wrapper element (SelectionPopup scope +
   *  JumpToTopButton hover ancestor). Owned by the parent, forwarded here. */
  setConversationEl: (el: HTMLDivElement | null) => void;
  /** Wrapper element the JumpToTopButton attaches its hover listeners to. */
  containerEl: HTMLElement | null;
  /** StickToBottom scroll container + lock controls, lifted by the bridge. */
  scroller: ConversationScroller | null;
  setScroller: (s: ConversationScroller | null) => void;
  /** Bumped on each local send so the list scrolls back to the bottom. */
  sendScrollNonce: number;
  hasMoreHistory: boolean;
  loadingMoreHistory: boolean;
  isMobileViewport: boolean;
  /** Display-only "Working…" gate (edge-driven, from the parent). */
  showsWorking: boolean;
  agentsError: unknown;
  /** True while a managed-sandbox launch is in flight (cold-launch spinner). */
  sandboxLaunching: boolean;
  /** Terminal-first spin-up bits for the cold-launch empty state. */
  terminalFirst: { isTerminalFirst: boolean; terminalStartingUp?: boolean } | null | undefined;
  /** Pub/sub ref for the LatestTurnSpacer's synchronous re-measure handle. */
  spacerMeasureRef: React.RefObject<(() => void) | null>;
}

export function isNativeFindShortcut(
  event: Pick<
    globalThis.KeyboardEvent,
    "altKey" | "ctrlKey" | "defaultPrevented" | "key" | "metaKey"
  >,
): boolean {
  return (
    !event.defaultPrevented &&
    !event.altKey &&
    event.key.toLowerCase() === "f" &&
    (event.metaKey || event.ctrlKey)
  );
}

/**
 * The scrolling transcript column: the ONLY subtree that subscribes to the
 * streaming-hot store fields (`blocks`, `activeResponse`, `pendingUserMessages`,
 * `interruptedResponseIds`, `sessionStatus`) and rebuilds the bubble list. It's
 * wrapped in `memo` and receives only edge-driven / stable props, so a streaming
 * frame re-renders this subtree alone — the composer, header, status bar, and
 * dialogs bail out via React's normal prop-equality check.
 */
function TranscriptImpl({
  setConversationEl,
  containerEl,
  scroller,
  setScroller,
  sendScrollNonce,
  hasMoreHistory,
  loadingMoreHistory,
  isMobileViewport,
  showsWorking,
  agentsError,
  sandboxLaunching,
  terminalFirst,
  spacerMeasureRef,
}: TranscriptProps) {
  const blocks = useChatStore((s) => s.blocks);
  const pendingUserMessages = useChatStore((s) => s.pendingUserMessages);
  const activeResponse = useChatStore((s) => s.activeResponse);
  const interruptedResponseIds = useChatStore((s) => s.interruptedResponseIds);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const subagentRoutingOverride = useChatStore((s) => s.subagentRoutingOverride);
  const mcpStartupActive = useChatStore((s) => s.mcpStartup !== null);
  const codexStartupPromptActive = useChatStore((s) => s.codexStartupPrompt !== null);
  const hasTasks = useChatStore((s) => s.todos.length > 0);
  const conversationId = useChatStore((s) => s.conversationId);

  // Build bubbles once per blocks/activeResponse change. Per-surface reuse
  // cache so a streaming append rebuilds only the active bubble, reusing the
  // finalized prefix by reference. Pending user messages (POSTed but not yet
  // acked) render as trailing user bubbles so the input is visible immediately.
  const bubbleCacheRef = useRef<BubbleCache>(createBubbleCache());
  const bubbles = useMemo<Bubble[]>(() => {
    const committed = stripGatedSubagentRoutingChips(
      reorderCommittedRequestElicitations(
        buildBubbles(
          blocks,
          activeResponse,
          bubbleCacheRef.current,
          interruptedResponseIds,
          computeIsWorking(sessionStatus),
        ),
      ),
      subagentRoutingOverride,
    );
    if (pendingUserMessages.length === 0) return committed;
    return mergePendingBubbles(
      committed,
      buildPendingBubbles(pendingUserMessages, getCurrentAuthorId()),
    );
  }, [
    blocks,
    activeResponse,
    interruptedResponseIds,
    pendingUserMessages,
    subagentRoutingOverride,
    sessionStatus,
  ]);

  const pendingElicitations = useMemo(() => collectPendingElicitations(bubbles), [bubbles]);
  const streamBubbles = useMemo(
    () => (pendingElicitations.length === 0 ? bubbles : stripPendingElicitations(bubbles)),
    [bubbles, pendingElicitations.length],
  );
  const committedUserIds = useMemo(() => {
    const ids = new Set<string>();
    for (const block of blocks) {
      if (
        block.type === "user_message" &&
        !isSystemUserContent(block.content) &&
        block.ctx.itemId !== null
      ) {
        ids.add(block.ctx.itemId);
      }
    }
    return ids;
  }, [blocks]);
  // Keep every bubble-derived surface on one coherent model so the
  // rail/spacer/indicators always describe the rows being rendered.
  const display = useMemo(
    () => ({
      conversationId,
      bubbles,
      streamBubbles,
      pendingElicitations,
      blockCount: blocks.length,
      committedUserIds,
      hasCommittedAnchor:
        committedUserIds.size > 0 || blocks.some((block) => block.type !== "user_message"),
      hasTasks,
      mcpStartupActive,
      codexStartupPromptActive,
      hasMoreHistory,
      loadingMoreHistory,
      showsWorking,
    }),
    [
      conversationId,
      bubbles,
      streamBubbles,
      pendingElicitations,
      blocks,
      committedUserIds,
      hasTasks,
      mcpStartupActive,
      codexStartupPromptActive,
      hasMoreHistory,
      loadingMoreHistory,
      showsWorking,
    ],
  );
  const [nativeFindConversationId, setNativeFindConversationId] = useState<string | null>(null);
  useEffect(() => setNativeFindConversationId(null), [conversationId]);
  useEffect(() => {
    const handleFind = (event: globalThis.KeyboardEvent) => {
      if (!display.conversationId || !isNativeFindShortcut(event)) return;
      // Native find runs after keydown dispatch. Commit every row before the
      // browser scans the DOM so off-screen messages participate.
      flushSync(() => setNativeFindConversationId(display.conversationId));
    };
    window.addEventListener("keydown", handleFind);
    return () => window.removeEventListener("keydown", handleFind);
  }, [display.conversationId]);
  const disableVirtualization = nativeFindConversationId === display.conversationId;

  // Virtualizer-derived geometry (scroll handle, active turn, range nonce),
  // published by VirtualBubbleList. The rail reads the active turn and the
  // spacer the range nonce, all from the virtualizer's model rather than the
  // windowed DOM. `scrollToItem` is held in a ref so `ensureItemVisible` keeps a
  // stable identity; the reactive fields are lifted to state.
  const scrollToItemRef = useRef<((itemId: string) => boolean) | null>(null);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [spacerMeasureNonce, setSpacerMeasureNonce] = useState(0);
  const onGeometryChange = useCallback((geometry: TranscriptGeometry) => {
    scrollToItemRef.current = geometry.scrollToItem;
    setActiveTurnId(geometry.activeTurnId);
    setSpacerMeasureNonce(geometry.rangeNonce);
  }, []);
  const ensureItemVisible = useCallback(
    (itemId: string) => scrollToItemRef.current?.(itemId) ?? false,
    [],
  );

  // Single nav instance shared by hotkey + buttons. System-message bubbles are
  // excluded — the hotkey is for navigating real user turns, not markers.
  const userMessageIds = useMemo(
    () =>
      display.bubbles
        .filter(
          (b): b is Extract<Bubble, { kind: "user" }> => b.kind === "user" && !isSystemBubble(b),
        )
        .map((b) => b.itemId),
    [display.bubbles],
  );
  const nav = useUserMessageNav(userMessageIds, ensureItemVisible);

  // One rail tick per real user turn, paired with a preview of the reply that
  // followed. Mirrors the transcript's loaded window and grows lazily.
  const turns = useMemo<Turn[]>(() => {
    const out: Turn[] = [];
    for (let i = 0; i < display.bubbles.length; i++) {
      const b = display.bubbles[i];
      if (b.kind !== "user" || isSystemBubble(b)) continue;
      let preview = "";
      for (let j = i + 1; j < display.bubbles.length; j++) {
        const next = display.bubbles[j];
        if (next.kind === "user" && !isSystemBubble(next)) break;
        if (next.kind === "assistant") {
          const textItem = next.items.find((it) => it.kind === "text" && it.text.trim());
          if (textItem && textItem.kind === "text") {
            preview = textItem.text.trim();
            break;
          }
        }
      }
      out.push({
        itemId: b.itemId,
        userText: extractUserText(b.content),
        responsePreview: preview.slice(0, 240),
      });
    }
    return out;
  }, [display.bubbles]);

  const lastAssistantIndex = useMemo(
    () => liveCandidateAssistantIndex(display.streamBubbles),
    [display.streamBubbles],
  );

  // Cmd+Alt+↑/↓ (Ctrl+Alt on win/linux) user-turn navigation.
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent) => {
      if (!hasCommandModifier(e) || !e.altKey) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      if (e.defaultPrevented) return;
      e.preventDefault();
      if (e.key === "ArrowUp") nav.goPrev();
      else nav.goNext();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [nav]);

  const showWorkingIndicator = shouldShowWorkingIndicator(display.showsWorking, display.bubbles);
  return (
    <>
      {/* Task tracker pinned above the thread. Sibling of the viewport (not an
      overlay) so it shrinks the scroll area rather than covering messages.
      Self-hides with no tasks. */}
      <ChatPlanAccordion className="mt-14 md:mt-12" />
      {/* Wrapper div gives us a ref to scope the SelectionPopup to the
      conversation area without requiring Conversation to forward refs. */}
      <div
        ref={setConversationEl}
        className="@container/chat relative flex min-h-0 flex-1 overflow-hidden"
      >
        <Conversation className={cn(!display.hasTasks && "chat-scroll-fade", "flex-1")}>
          <ConversationContent
            scrollClassName="transcript-hide-native-scrollbar"
            className={cn(
              "chat-conversation-content mx-auto w-full gap-4 px-4 pb-6",
              display.hasTasks ? "pt-4" : "pt-20",
              "md:pl-[clamp(1rem,(54rem-100cqi)*0.5+1rem,1.5rem)]",
              CHAT_COLUMN_WIDTH,
            )}
          >
            {/* Scroll helpers — must live inside StickToBottom to access context. */}
            <ScrollToBottomOnSend nonce={sendScrollNonce} />
            <KeepBottomOnViewportResize />
            <ConversationScrollRefBridge onScroller={setScroller} />
            <HistoryAutoLoader scrollElement={scroller?.el ?? null} />
            {display.bubbles.length === 0 &&
            !showWorkingIndicator &&
            !display.mcpStartupActive &&
            !display.codexStartupPromptActive ? (
              (terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp) ||
              sandboxLaunching ? (
                <RunnerStartingIndicator variant="hero" />
              ) : (
                <ConversationEmptyState>
                  <div className="space-y-1.5">
                    <h3 className="text-2xl font-medium tracking-[-0.02em]">
                      What should we work on?
                    </h3>
                    <p className="text-muted-foreground text-ui">
                      {agentsError
                        ? `Failed to load agents: ${agentsError instanceof Error ? agentsError.message : String(agentsError)}`
                        : "Send a message to get started."}
                    </p>
                  </div>
                </ConversationEmptyState>
              )
            ) : (
              <>
                {/* Older pages prepend here while their request is in flight. */}
                {display.loadingMoreHistory && <HistoryLoadingIndicator />}
                <VirtualBubbleList
                  bubbles={display.streamBubbles}
                  scrollEl={scroller?.el ?? null}
                  lastAssistantIndex={lastAssistantIndex}
                  showsWorking={display.showsWorking}
                  sessionIdle={sessionStatus === "idle"}
                  conversationId={display.conversationId}
                  hasTasks={display.hasTasks}
                  disableVirtualization={disableVirtualization}
                  onGeometryChange={onGeometryChange}
                />
                {/* Pending elicitation cards, floated to the bottom of the chat
                so an outstanding question stays in view. Newest renders last,
                nearest the composer. Above the Working… indicator. */}
                {display.pendingElicitations.map((item) => (
                  <Message
                    key={item.elicitationId}
                    from="assistant"
                    className="max-w-full"
                    data-testid="bottom-elicitation"
                  >
                    <MessageContent className="w-full">
                      <ElicitationCard item={item} />
                    </MessageContent>
                  </Message>
                ))}
                {/* Working… shimmer, lit for the whole busy turn. */}
                {showWorkingIndicator && <WorkingIndicator />}
                {/* Terminal-first spin-up cue; self-gates to null off the
                spin-up window, and only when not already showing Working…. */}
                {!showWorkingIndicator && <RunnerStartingIndicator variant="row" />}
                {/* MCP-server startup band (codex-native); clears once the
                round settles (failures stay in host logs, not the chat). */}
                <McpStartupIndicator />
                {/* Codex parked on an interactive terminal startup prompt
                (trust / hooks / TERM); clears once the thread starts. */}
                <CodexStartupPromptIndicator />
              </>
            )}
            {/* Frames the initially loaded turn at the top of the viewport. */}
            <LatestTurnSpacer
              key={display.conversationId ?? "landing"}
              scrollElement={scroller?.el ?? null}
              conversationId={display.conversationId}
              blockCount={display.blockCount}
              committedUserIds={display.committedUserIds}
              hasCommittedAnchor={display.hasCommittedAnchor}
              topGapPx={display.hasTasks ? 16 : undefined}
              measureRef={spacerMeasureRef}
              remeasureNonce={spacerMeasureNonce}
            />
          </ConversationContent>
          <ConversationScrollButton />
          <UserMessageNavConnected
            goPrev={nav.goPrev}
            goNext={nav.goNext}
            canPrev={nav.canPrev}
            canNext={nav.canNext}
            hidden={userMessageIds.length === 0}
          />
        </Conversation>
        {/* Constant-height scrollbar. Sibling of Conversation so it escapes the
        chat-scroll-fade mask. */}
        <TranscriptScrollbar scroller={scroller} topInset={display.hasTasks ? 12 : undefined} />
        {/* Hover the top edge to reveal a pill that loads all older history. */}
        <JumpToTopButton
          containerEl={containerEl}
          scroller={scroller}
          hasMoreHistory={display.hasMoreHistory}
        />
        {/* Too-many-tabs warning, a sibling of Conversation. */}
        <StreamBudgetBanner />
        {/* Left-edge minimap: one tick per turn. Desktop-only. */}
        {!isMobileViewport && (
          <TurnRail
            turns={turns}
            hasMoreHistory={display.hasMoreHistory}
            loadingMoreHistory={display.loadingMoreHistory}
            ensureItemVisible={ensureItemVisible}
            activeTurnId={activeTurnId}
          />
        )}
      </div>
    </>
  );
}

/**
 * The user turn that owns a given bubble index: the nearest user bubble at or
 * before it (a turn spans from its user bubble to the next). Falls back to the
 * first user turn when the index sits above every one. Pure so the active-tick
 * mapping is testable without the virtualizer, which supplies `midIndex` via
 * `getVirtualItemForOffset` — a lookup over ALL items, so it resolves a turn
 * whose row is windowed out above or below the viewport just the same.
 */
export function activeTurnIdAtBubbleIndex(bubbles: Bubble[], midIndex: number): string | null {
  const isUserTurn = (b: Bubble): b is Extract<Bubble, { kind: "user" }> =>
    b.kind === "user" && !isSystemBubble(b);
  if (bubbles.length === 0) return null;
  const start = Math.min(Math.max(midIndex, 0), bubbles.length - 1);
  for (let i = start; i >= 0; i--) {
    const b = bubbles[i];
    if (b && isUserTurn(b)) return b.itemId;
  }
  return bubbles.find(isUserTurn)?.itemId ?? null;
}

/**
 * Windows the bubble list off the StickToBottom scroll container so only the
 * on-screen slice mounts — switching into a long conversation no longer pays to
 * mount every bubble's markdown/tool subtree at once.
 *
 * The window is one flex child of the content div, sized to the full measured
 * height (`getTotalSize`) with each row absolutely positioned. Keeping the
 * wrapper at full height preserves `scrollHeight`, so StickToBottom's
 * at-bottom math, the TranscriptScrollbar, and the LatestTurnSpacer all keep
 * measuring the whole transcript even though most rows are unmounted. Rows are
 * keyed by `bubbleKey` so measured heights follow a bubble across a streaming
 * rebuild or a history prepend.
 */
/**
 * Transcript geometry derived from the virtualizer's model (all items, mounted
 * or not) — the single source of truth the rail and spacer read instead of
 * scanning the windowed DOM.
 */
export interface TranscriptGeometry {
  /** Pulls a turn's row into the mounted window; false if its id isn't found. */
  scrollToItem: (itemId: string) => boolean;
  /** itemId of the user turn owning the viewport midpoint, or null. */
  activeTurnId: string | null;
  /** Bumps whenever the mounted range changes, so the spacer can re-measure a
   *  windowed-out anchor that has since remounted. */
  rangeNonce: number;
}

export type TranscriptViewSnapshot =
  | { atBottom: true }
  | {
      atBottom: false;
      anchorKey: string | null;
      anchorOffset: number;
      fallbackOffset: number;
    };

export function captureTranscriptViewSnapshot({
  atBottom,
  scrollTop,
  anchor,
}: {
  atBottom: boolean;
  scrollTop: number;
  anchor?: { key: string; start: number };
}): TranscriptViewSnapshot {
  if (atBottom) return { atBottom: true };
  return {
    atBottom: false,
    anchorKey: anchor?.key ?? null,
    anchorOffset: anchor ? anchor.start - scrollTop : 0,
    fallbackOffset: Math.round(scrollTop),
  };
}

export function resolveTranscriptViewOffset(
  snapshot: Exclude<TranscriptViewSnapshot, { atBottom: true }>,
  getAnchorStart: (key: string) => number | undefined,
): number {
  const anchorStart = snapshot.anchorKey === null ? undefined : getAnchorStart(snapshot.anchorKey);
  return anchorStart === undefined
    ? snapshot.fallbackOffset
    : Math.max(0, anchorStart - snapshot.anchorOffset);
}

const MAX_CACHED_VIEWS = 24;
const transcriptViewCache = new Map<string, TranscriptViewSnapshot>();
function rememberTranscriptView(convId: string, snap: TranscriptViewSnapshot): void {
  transcriptViewCache.delete(convId); // re-insert to refresh LRU order
  transcriptViewCache.set(convId, snap);
  while (transcriptViewCache.size > MAX_CACHED_VIEWS) {
    const oldest = transcriptViewCache.keys().next().value;
    if (oldest === undefined) break;
    transcriptViewCache.delete(oldest);
  }
}
/** Physical "is this scroll element at (or within a hair of) its bottom". */
const BOTTOM_EPSILON_PX = 8;
const RESTORE_CANCEL_EVENTS = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
function isElAtBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.clientHeight - el.scrollTop <= BOTTOM_EPSILON_PX;
}

export function VirtualBubbleList({
  bubbles,
  scrollEl,
  lastAssistantIndex,
  showsWorking,
  sessionIdle,
  conversationId,
  hasTasks,
  disableVirtualization,
  onGeometryChange,
}: {
  bubbles: Bubble[];
  scrollEl: HTMLElement | null;
  lastAssistantIndex: number;
  showsWorking: boolean;
  sessionIdle: boolean;
  conversationId: string | null | undefined;
  hasTasks: boolean;
  disableVirtualization: boolean;
  /** Publishes virtualizer-derived geometry up to the rail/spacer. */
  onGeometryChange: (geometry: TranscriptGeometry) => void;
}) {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    stopScroll: () => void;
    state: { isAtBottom: boolean; escapedFromLock: boolean };
  };
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;
  const storeConvId = useChatStore((s) => s.conversationId);
  const restoringRef = useRef<string | null>(null);
  const latestSnapshotRef = useRef<{
    conversationId: string;
    snapshot: TranscriptViewSnapshot;
  } | null>(null);
  const lastMessageIndex = useMemo(
    () =>
      bubbles.findLastIndex(
        (bubble) =>
          bubble.kind === "assistant" || (bubble.kind === "user" && !isSystemBubble(bubble)),
      ),
    [bubbles],
  );

  const wrapperRef = useRef<HTMLDivElement>(null);
  // The list isn't the scroll container's first child — indicators, padding,
  // and the task tracker sit above it — so its top offset feeds the virtualizer
  // as scrollMargin. Without it every row's computed `start` is shifted and the
  // wrong window mounts. It goes stale whenever content ABOVE the list changes
  // height without changing `bubbles.length` (the history-loading indicator
  // toggling, the `pt-4 ↔ pt-20` task padding), so it is remeasured by watching
  // the content element — which reflows on any such change — not just the
  // scroll container (whose box size those changes leave untouched).
  const [scrollMargin, setScrollMargin] = useState(0);
  useLayoutEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper || !scrollEl) return;
    const measure = () => {
      const offset =
        wrapper.getBoundingClientRect().top -
        scrollEl.getBoundingClientRect().top +
        scrollEl.scrollTop;
      setScrollMargin((prev) => (Math.abs(prev - offset) >= 1 ? offset : prev));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(scrollEl); // viewport height changes
    if (wrapper.parentElement) observer.observe(wrapper.parentElement); // content reflow above
    return () => observer.disconnect();
  }, [scrollEl, bubbles.length, hasTasks]);

  const virtualizer = useVirtualizer({
    enabled: !disableVirtualization,
    count: bubbles.length,
    getScrollElement: () => scrollEl,
    // Corrected per row by measureElement; a middling bubble keeps the initial
    // total close enough that the first paint doesn't jump.
    estimateSize: () => 280,
    getItemKey: (index) => bubbleKey(bubbles[index]!),
    // Replaces the content column's `gap-4` between bubbles, which absolute
    // positioning would otherwise drop.
    gap: 16,
    overscan: 6,
    scrollMargin,
  });

  // Latest bubbles/virtualizer read through refs so published callbacks keep a
  // stable identity across renders.
  const bubblesRef = useRef(bubbles);
  bubblesRef.current = bubbles;
  const virtualizerRef = useRef(virtualizer);
  virtualizerRef.current = virtualizer;

  // Persist the item crossing the viewport top, not only a raw scrollTop.
  // Virtual row estimates can change while a conversation is hidden; the key
  // lets restoration follow the same bubble as those estimates settle.
  useEffect(() => {
    if (!scrollEl || !conversationId) return;
    const save = () => {
      if (storeConvId !== conversationId || restoringRef.current === conversationId) return;
      if (isElAtBottom(scrollEl)) {
        const snapshot = { atBottom: true } as const;
        latestSnapshotRef.current = { conversationId, snapshot };
        rememberTranscriptView(conversationId, snapshot);
        return;
      }
      const top = scrollEl.scrollTop;
      const anchor = virtualizerRef.current.getVirtualItemForOffset(top);
      const bubble = anchor ? bubblesRef.current[anchor.index] : undefined;
      const snapshot = captureTranscriptViewSnapshot({
        atBottom: false,
        scrollTop: top,
        anchor: anchor && bubble ? { key: bubbleKey(bubble), start: anchor.start } : undefined,
      });
      latestSnapshotRef.current = { conversationId, snapshot };
      rememberTranscriptView(conversationId, snapshot);
    };
    save();
    scrollEl.addEventListener("scroll", save, { passive: true });
    return () => {
      const latest = latestSnapshotRef.current;
      if (latest?.conversationId === conversationId) {
        rememberTranscriptView(conversationId, latest.snapshot);
      }
      scrollEl.removeEventListener("scroll", save);
    };
  }, [conversationId, scrollEl, storeConvId]);

  // Restore only when the coherent deferred snapshot changes conversation.
  // Bottom mode is written synchronously before StickToBottom takes over.
  // Mid-scroll mode resolves the saved bubble through the virtualizer on every
  // frame, so estimate-to-measure corrections preserve its viewport position.
  useLayoutEffect(() => {
    if (!scrollEl || !conversationId) return;
    const c = ctxRef.current;
    const saved = transcriptViewCache.get(conversationId);
    restoringRef.current = conversationId;
    let frame = 0;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      if (restoringRef.current === conversationId) restoringRef.current = null;
      cancelAnimationFrame(frame);
      for (const type of RESTORE_CANCEL_EVENTS) {
        scrollEl.removeEventListener(type, finish);
      }
    };

    if (!saved || saved.atBottom) {
      const pinBottom = () => {
        c.state.isAtBottom = true;
        c.state.escapedFromLock = false;
        scrollEl.scrollTop = Math.max(0, scrollEl.scrollHeight - scrollEl.clientHeight - 1);
        void c.scrollToBottom("instant");
      };
      pinBottom();
      // Runs after sibling layout effects (notably LatestTurnSpacer) but before
      // the browser's next paint.
      queueMicrotask(() => {
        if (!done && restoringRef.current === conversationId) pinBottom();
      });
      frame = requestAnimationFrame(() => {
        pinBottom();
        frame = requestAnimationFrame(finish);
      });
      return finish;
    }

    c.stopScroll();
    const deadline = performance.now() + SCROLL_RESTORE_BUDGET_MS;
    const pinAnchor = () => {
      scrollEl.scrollTop = resolveTranscriptViewOffset(saved, (key) => {
        const index = bubblesRef.current.findIndex((bubble) => bubbleKey(bubble) === key);
        return index < 0
          ? undefined
          : virtualizerRef.current.getOffsetForIndex(index, "start")?.[0];
      });
    };
    const tick = () => {
      if (done || restoringRef.current !== conversationId) return;
      if (performance.now() >= deadline) {
        finish();
        return;
      }
      pinAnchor();
      frame = requestAnimationFrame(tick);
    };
    for (const type of RESTORE_CANCEL_EVENTS) {
      scrollEl.addEventListener(type, finish, { passive: true });
    }
    pinAnchor();
    frame = requestAnimationFrame(tick);
    return finish;
  }, [conversationId, scrollEl]);

  const scrollToItem = useCallback((itemId: string): boolean => {
    const index = bubblesRef.current.findIndex((b) => b.kind === "user" && b.itemId === itemId);
    if (index < 0) return false;
    virtualizerRef.current.scrollToIndex(index, { align: "center" });
    return true;
  }, []);

  const totalSize = virtualizer.getTotalSize();
  const range = virtualizer.range;

  // The user turn owning the viewport midpoint, from the virtualizer's model —
  // NOT the windowed DOM. `getVirtualItemForOffset` maps a scroll offset to a
  // bubble index across ALL items (mounted or not), so a turn whose row is
  // unmounted above OR below the viewport is still resolved correctly; the
  // active turn is the nearest user bubble at or before that index.
  const activeTurnId = useMemo(() => {
    const scrollOffset = virtualizer.scrollOffset;
    if (scrollOffset === null || bubbles.length === 0) return null;
    const viewport = scrollEl?.clientHeight ?? 0;
    const midItem = virtualizer.getVirtualItemForOffset(scrollOffset + viewport / 2);
    return activeTurnIdAtBubbleIndex(bubbles, midItem?.index ?? bubbles.length - 1);
    // `range` and `totalSize` are deps so the active turn recomputes as the
    // window scrolls and as measurements settle; `scrollOffset` alone isn't a
    // render trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bubbles, scrollEl, range, totalSize, virtualizer]);

  // Older-history prepend compensation. Absolute rows are out of normal flow,
  // so the browser's native scroll anchoring — which `HistoryAutoLoader` leans
  // on to hold the read position across a prepend — can't act on them. Rather
  // than compensate by the total-height delta (which double-counts any change
  // ABOVE the list in the same commit, e.g. the HistoryLoadingIndicator being
  // removed), capture a still-present anchor row's offset before the prepend
  // and restore scrollTop so that row sits at the same viewport position after.
  // Measured purely from the virtualizer, so content above the list is
  // irrelevant. react-virtual then corrects the estimate→actual delta itself as
  // the freshly-mounted top rows measure.
  const prevFirstKeyRef = useRef<string | undefined>(undefined);
  // Snapshot of the previous render's first mounted row (top of the window,
  // nearest an incoming prepend): its key and the offset it held THEN. The
  // effect reads this (still the pre-prepend value) before overwriting it with
  // the current render's snapshot, so a prepend restores that row to the same
  // viewport position.
  const anchorSnapshotRef = useRef<{ key: string; offset: number } | null>(null);
  const firstVisible = virtualizer.getVirtualItems()[0];
  const currentSnapshot =
    firstVisible && typeof firstVisible.key === "string"
      ? { key: firstVisible.key, offset: firstVisible.start }
      : null;
  useLayoutEffect(() => {
    const firstKey = bubbles.length > 0 ? bubbleKey(bubbles[0]!) : undefined;
    const prevFirstKey = prevFirstKeyRef.current;
    const prevSnapshot = anchorSnapshotRef.current;
    prevFirstKeyRef.current = firstKey;
    anchorSnapshotRef.current = currentSnapshot;
    // Only a prepend (grew at the top, old first key still present). A switch
    // replaces the list (old first key gone) — handled by mount scroll-to-
    // bottom. Streaming appends leave the first key unchanged.
    if (!scrollEl || prevFirstKey === undefined || firstKey === prevFirstKey || !prevSnapshot) {
      return;
    }
    if (!bubbles.some((b) => bubbleKey(b) === prevFirstKey)) return;
    // Where the pre-prepend anchor row sits now, by key (its index shifted by
    // the prepend). Restore scrollTop so it holds its pre-prepend viewport
    // offset — measured purely from the virtualizer, so anything removed ABOVE
    // the list in the same commit (the HistoryLoadingIndicator) doesn't matter.
    const newIndex = bubbles.findIndex((b) => bubbleKey(b) === prevSnapshot.key);
    if (newIndex < 0) return;
    const newStart = virtualizer.getOffsetForIndex(newIndex, "start")?.[0];
    if (newStart === undefined) return;
    const delta = newStart - prevSnapshot.offset;
    if (delta > 0 && scrollEl.scrollTop > 1) scrollEl.scrollTop += delta;
    // currentSnapshot is intentionally captured per render; the effect only
    // needs the previous one, stored above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bubbles, scrollEl, virtualizer]);

  // Publish geometry (scroll handle, active turn, range nonce) up to the rail
  // and spacer. rangeNonce bumps on any mounted-range change so a windowed-out
  // spacer anchor that has remounted gets a fresh measure.
  const rangeNonce = (range?.startIndex ?? -1) * 100003 + (range?.endIndex ?? -1);
  useEffect(() => {
    onGeometryChange({ scrollToItem, activeTurnId, rangeNonce });
  }, [onGeometryChange, scrollToItem, activeTurnId, rangeNonce]);

  if (disableVirtualization) {
    return (
      <div className="flex w-full flex-col gap-4">
        {bubbles.map((bubble, index) => (
          <BubbleView
            key={bubbleKey(bubble)}
            bubble={bubble}
            isLastAssistant={index === lastAssistantIndex}
            showsWorking={showsWorking && index === lastAssistantIndex}
            actionsPersistent={
              index === lastMessageIndex && bubble.kind === "assistant" && sessionIdle
            }
          />
        ))}
      </div>
    );
  }

  return (
    <div ref={wrapperRef} className="relative w-full" style={{ height: `${totalSize}px` }}>
      {virtualizer.getVirtualItems().map((item) => {
        const bubble = bubbles[item.index];
        if (!bubble) return null;
        return (
          <div
            key={item.key}
            data-index={item.index}
            data-bubble-key={bubbleKey(bubble)}
            ref={virtualizer.measureElement}
            className="absolute top-0 left-0 w-full"
            style={{ transform: `translateY(${item.start - scrollMargin}px)` }}
          >
            <BubbleView
              bubble={bubble}
              isLastAssistant={item.index === lastAssistantIndex}
              showsWorking={showsWorking && item.index === lastAssistantIndex}
              actionsPersistent={
                item.index === lastMessageIndex && bubble.kind === "assistant" && sessionIdle
              }
            />
          </div>
        );
      })}
    </div>
  );
}

export const Transcript = memo(TranscriptImpl);
