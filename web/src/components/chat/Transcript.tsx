import { memo, useEffect, useMemo, useRef } from "react";
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
import { RunnerStartingIndicator, McpStartupIndicator } from "@/pages/ChatIndicators";
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

export interface TranscriptProps {
  /**
   * Deferred conversation id — keys the `<Conversation>` subtree so it
   * remounts at transition priority rather than blocking the interaction frame.
   * Passed from ChatPage as `useDeferredValue(urlConvId)`.
   */
  conversationKey: string | null | undefined;
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

/**
 * The scrolling transcript column: the ONLY subtree that subscribes to the
 * streaming-hot store fields (`blocks`, `activeResponse`, `pendingUserMessages`,
 * `interruptedResponseIds`, `sessionStatus`) and rebuilds the bubble list. It's
 * wrapped in `memo` and receives only edge-driven / stable props, so a streaming
 * frame re-renders this subtree alone — the composer, header, status bar, and
 * dialogs bail out via React's normal prop-equality check.
 */
function TranscriptImpl({
  conversationKey,
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
  const hasTasks = useChatStore((s) => s.todos.length > 0);

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

  // Single nav instance shared by hotkey + buttons. System-message bubbles are
  // excluded — the hotkey is for navigating real user turns, not markers.
  const userMessageIds = useMemo(
    () =>
      bubbles
        .filter(
          (b): b is Extract<Bubble, { kind: "user" }> => b.kind === "user" && !isSystemBubble(b),
        )
        .map((b) => b.itemId),
    [bubbles],
  );
  const nav = useUserMessageNav(userMessageIds);

  // One rail tick per real user turn, paired with a preview of the reply that
  // followed. Mirrors the transcript's loaded window and grows lazily.
  const turns = useMemo<Turn[]>(() => {
    const out: Turn[] = [];
    for (let i = 0; i < bubbles.length; i++) {
      const b = bubbles[i];
      if (b.kind !== "user" || isSystemBubble(b)) continue;
      let preview = "";
      for (let j = i + 1; j < bubbles.length; j++) {
        const next = bubbles[j];
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
  }, [bubbles]);

  // Pending elicitation cards float to the bottom of the chat: rendered as the
  // last items and removed from their inline slot so they don't render twice.
  // `streamBubbles` keeps `bubbles`' reference when nothing is pending.
  const pendingElicitations = useMemo(() => collectPendingElicitations(bubbles), [bubbles]);
  const streamBubbles = useMemo(
    () => (pendingElicitations.length === 0 ? bubbles : stripPendingElicitations(bubbles)),
    [bubbles, pendingElicitations.length],
  );
  const lastAssistantIndex = useMemo(
    () => liveCandidateAssistantIndex(streamBubbles),
    [streamBubbles],
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

  const showWorkingIndicator = shouldShowWorkingIndicator(showsWorking, bubbles);

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
        <Conversation
          key={conversationKey ?? "landing"}
          className={cn(!hasTasks && "chat-scroll-fade", "flex-1")}
        >
          <ConversationContent
            scrollClassName="transcript-hide-native-scrollbar"
            className={cn(
              "chat-conversation-content mx-auto w-full gap-4 px-4 pb-6",
              hasTasks ? "pt-4" : "pt-20",
              "md:pl-[clamp(1rem,(54rem-100cqi)*0.5+1rem,1.5rem)]",
              CHAT_COLUMN_WIDTH,
            )}
          >
            {/* Scroll helpers — must live inside StickToBottom to access context. */}
            <ScrollToBottomOnSend nonce={sendScrollNonce} />
            <KeepBottomOnViewportResize />
            <ConversationScrollRefBridge onScroller={setScroller} />
            <HistoryAutoLoader scrollElement={scroller?.el ?? null} />
            {bubbles.length === 0 && !showWorkingIndicator && !mcpStartupActive ? (
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
                {loadingMoreHistory && <HistoryLoadingIndicator />}
                {streamBubbles.map((bubble, bubbleIndex) => (
                  <BubbleView
                    key={bubbleKey(bubble)}
                    bubble={bubble}
                    isLastAssistant={bubbleIndex === lastAssistantIndex}
                    showsWorking={showsWorking && bubbleIndex === lastAssistantIndex}
                  />
                ))}
                {/* Pending elicitation cards, floated to the bottom of the chat
                so an outstanding question stays in view. Newest renders last,
                nearest the composer. Above the Working… indicator. */}
                {pendingElicitations.map((item) => (
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
              </>
            )}
            {/* Frames the initially loaded turn at the top of the viewport. */}
            <LatestTurnSpacer
              scrollElement={scroller?.el ?? null}
              topGapPx={hasTasks ? 16 : undefined}
              measureRef={spacerMeasureRef}
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
        <TranscriptScrollbar scroller={scroller} topInset={hasTasks ? 12 : undefined} />
        {/* Hover the top edge to reveal a pill that loads all older history. */}
        <JumpToTopButton
          containerEl={containerEl}
          scroller={scroller}
          hasMoreHistory={hasMoreHistory}
        />
        {/* Too-many-tabs warning, a sibling of Conversation. */}
        <StreamBudgetBanner />
        {/* Left-edge minimap: one tick per turn. Desktop-only. */}
        {!isMobileViewport && (
          <TurnRail
            turns={turns}
            scroller={scroller}
            hasMoreHistory={hasMoreHistory}
            loadingMoreHistory={loadingMoreHistory}
          />
        )}
      </div>
    </>
  );
}

export const Transcript = memo(TranscriptImpl);
