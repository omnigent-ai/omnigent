import { SelectionPopup } from "./SelectionPopup";
import { SideChatSettings } from "./SideChatSettings";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type KeyboardEvent,
} from "react";
import { MessagesSquareIcon, TriangleAlertIcon } from "lucide-react";
import { toast } from "sonner";
import { useIsMutating, useQueryClient } from "@tanstack/react-query";
import { getCurrentAuthorId } from "@/lib/identity";
import {
  type Bubble,
  type BubbleCache,
  buildBubbles,
  createBubbleCache,
  liveCandidateAssistantIndex,
} from "@/lib/renderItems";
import {
  BubbleView,
  WorkingIndicator,
  bubbleKey,
  buildPendingBubbles,
  computeIsWorking,
  computeIsTurnActive,
  mergePendingBubbles,
  reorderCommittedRequestElicitations,
  shouldShowWorkingIndicator,
  stripGatedSubagentRoutingChips,
} from "@/components/chat/chatBubbleParts";
import {
  ChatComposer,
  ComposerFeedbackRow,
  ComposerSendButton,
  type ComposerKeyIntent,
} from "@/components/composer/ChatComposer";
import { ComposerAddMenu } from "@/components/composer/ComposerAddMenu";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import {
  BUILTIN_SLASH_COMMANDS,
  SlashCommandMenu,
  isSlashCommandText,
  matchSlashCommandInvocation,
  skillDisplayNames,
  skillMenuDescription,
} from "@/components/SlashCommandMenu";
import { ComposerAttachments } from "@/components/ComposerAttachments";
import { ReplyDraftBlocks } from "@/components/composer/ReplyDraftBlocks";
import { Button } from "@/components/ui/button";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  EMPTY_SIDE_CHAT_COMPOSER,
  ensureConversationStreamed,
  holdsInitialDraft,
  isCompactControl,
  rejectBusyCompact,
  restoredSendDraftUnchanged,
  takeFailedSendDraft,
  type SideChatComposerDraft,
  useChatStore,
} from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { QueuedMessagesStrip } from "@/pages/QueuedMessagesStrip";
import { readAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { shouldQueueSend } from "@/lib/messageQueue";
import { attachmentAccept, clipboardFiles, validateAttachments } from "@/lib/attachments";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useDictationInsert } from "@/hooks/useDictationInsert";
import { useSession } from "@/hooks/useSession";
import type { Session } from "@/lib/types";
import { useSkills } from "@/hooks/useSkills";
import { useSlashCompletion } from "@/hooks/useSlashCompletion";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { skillInvocationPrefix } from "@/lib/harnessSetup";
import { isNativeTerminalSession } from "@/lib/nativeCodingAgents";
import { sideChatSettingsMutationKey, usesNativeSideChatFork } from "@/lib/sideChat";
import { parentIdFromLabels } from "@/lib/sessionHost";
import {
  appendReplyQuote,
  removeReplyQuote,
  restoreReplyDraft,
  serializeReplyDraft,
  snapshotReplyDraft,
} from "@/lib/replyDraft";
import { interrupt } from "@/lib/sessionsApi";
import { ConversationScopeContext } from "@/components/chat/conversationScope";

/** A `pending:` tab has no child session yet; its first send creates the fork. */
function isPendingSideChat(id: string): boolean {
  return id.startsWith("pending:");
}

// The forked-in item ids to hide are captured once per child and PERSISTED, so
// leaving the tab (which unmounts this pane) or reloading doesn't recapture the
// side chat's own completed turns as inherited history and blank the transcript.
// ponytail: one small localStorage key per child; entries are tiny id lists,
// orphaned on child deletion — prune only if this ever grows.
function inheritedBoundaryKey(childId: string): string {
  return `omnigent.sideChatInherited:${childId}`;
}
function readInheritedBoundary(childId: string): Set<string> | null {
  try {
    const raw = localStorage.getItem(inheritedBoundaryKey(childId));
    return raw ? new Set(JSON.parse(raw) as string[]) : null;
  } catch {
    return null;
  }
}
function writeInheritedBoundary(childId: string, ids: Set<string>): void {
  try {
    localStorage.setItem(inheritedBoundaryKey(childId), JSON.stringify([...ids]));
  } catch {
    // Storage disabled/full — the boundary just won't survive a reload.
  }
}

// Accurate for every harness: a side chat is a fork that stays out of the main
// thread. It is NOT reliably ephemeral — a non-Codex side chat is a persisted
// fork (hidden from the sidebar), so the copy doesn't promise it disappears.
const EMPTY_STATE_BODY = "Ask a question here without affecting the main conversation.";

/**
 * A scoped chat surface for a side-chat, rendered as a Workspace-rail tab beside
 * the still-active main chat.
 *
 * Two phases keyed by `childId`:
 * - **pending** (`pending:*`, no fork yet): shows the empty state + a composer;
 *   the first send calls `onStart`, which creates the fork.
 * - **live** (a real child conversation): streams the child's own registry entry
 *   (not the root store, which only projects the active conversation), reusing
 *   the main transcript's bubble pipeline. The forked-in history is hidden so
 *   the side chat starts visually empty — like Codex's native fork — while the
 *   agent still has the full context. Follow-ups send to the child via
 *   `pinnedConversationId`.
 *
 * @param childId The child conversation id, or a `pending:` placeholder.
 * @param onStart Create the fork from a pending tab's first message.
 */
export function SideChatPane({
  childId,
  selectionParentId,
  onStart,
  readOnly: restoredReadOnly = false,
  fullSession = false,
}: {
  childId: string;
  selectionParentId?: string;
  onStart?: (text: string) => Promise<void>;
  /** A dead, restored Codex side chat: show the transcript without a composer. */
  readOnly?: boolean;
  /** Display an existing session with its complete history and scoped composer. */
  fullSession?: boolean;
}) {
  const pending = isPendingSideChat(childId);
  const starting = useChatStore((s) => s.sideChatComposers[childId]?.starting ?? false);
  // The server seals a side chat whose fork died with its runner.
  const { session } = useSession(pending ? null : childId);
  const { session: draftSource } = useSession(pending ? (selectionParentId ?? null) : null);
  const readOnly = restoredReadOnly || session?.labels?.["omnigent.closed"] === "true";
  useLayoutEffect(() => {
    if (!selectionParentId || readOnly || starting) return;
    const target = { childId, parentId: selectionParentId };
    useChatStore.setState({ sideChatSelectionTarget: target });
    return () => {
      if (useChatStore.getState().sideChatSelectionTarget === target) {
        useChatStore.setState({ sideChatSelectionTarget: null });
      }
    };
  }, [childId, selectionParentId, readOnly, starting]);
  // Open the child's stream once (real tabs only) so it hydrates and streams
  // here. The store guards a double-bind and re-binds a failed entry, so
  // re-mounts / tab switches / retries are cheap.
  useEffect(() => {
    if (!pending) void ensureConversationStreamed(childId);
  }, [pending, childId]);
  // A real tab reads the child entry; a pending tab has none (null → empty).
  const state = useConversationEntryState(pending ? null : childId);
  const {
    blocks,
    activeResponse,
    interruptedResponseIds,
    pendingUserMessages,
    subagentRoutingOverride,
    sessionStatus,
    sessionHarness,
    boundAgentId,
    loadingConversation,
    conversationLoadError,
  } = state;

  // Hide the forked-in history: snapshot the item ids present once hydration
  // settles, then render only what arrives after (the side chat's own turns).
  // GENERIC forks only — they copy the parent transcript. A native Codex child
  // already holds only its own turns (context lives in the native thread), so
  // filtering there would wrongly hide the side chat's first question.
  const filterHistory = !fullSession && !usesNativeSideChatFork(sessionHarness);
  // Load the persisted boundary when the child changes (mount / rekey); the
  // in-mount ref avoids re-reading storage every render and tracks whether we've
  // actually observed a load run, so we don't capture a boundary on the initial
  // pre-load render.
  const boundaryRef = useRef<{
    childId: string;
    ids: Set<string> | null;
    loadSeen: boolean;
  } | null>(null);
  if (boundaryRef.current === null || boundaryRef.current.childId !== childId) {
    boundaryRef.current = {
      childId,
      ids: filterHistory && !pending ? readInheritedBoundary(childId) : null,
      loadSeen: false,
    };
  }
  if (loadingConversation) boundaryRef.current.loadSeen = true;
  // Capture once, when the fork's load has actually completed — even if it is
  // EMPTY (a parent with no history), so the side chat's first message isn't
  // mistaken for inherited history and hidden. Snapshot the inherited item ids
  // and persist so later mounts/reloads hide the same set, not new turns.
  if (
    boundaryRef.current.ids === null &&
    !pending &&
    filterHistory &&
    boundaryRef.current.loadSeen &&
    !loadingConversation &&
    conversationLoadError === null
  ) {
    const ids = new Set(
      blocks
        .map((b) => (b as { ctx?: { itemId?: string | null } }).ctx?.itemId)
        .filter((id): id is string => typeof id === "string"),
    );
    boundaryRef.current.ids = ids;
    writeInheritedBoundary(childId, ids);
  }
  const inherited = boundaryRef.current.ids;
  const visibleBlocks = useMemo(
    () =>
      inherited === null
        ? blocks
        : blocks.filter((b) => {
            const id = (b as { ctx?: { itemId?: string | null } }).ctx?.itemId;
            return !(typeof id === "string" && inherited.has(id));
          }),
    [blocks, inherited],
  );

  const bubbleCacheRef = useRef<BubbleCache>(createBubbleCache());
  const bubbles = useMemo<Bubble[]>(() => {
    const committed = stripGatedSubagentRoutingChips(
      reorderCommittedRequestElicitations(
        buildBubbles(
          visibleBlocks,
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
    visibleBlocks,
    activeResponse,
    interruptedResponseIds,
    pendingUserMessages,
    subagentRoutingOverride,
    sessionStatus,
  ]);

  const lastAssistantIndex = liveCandidateAssistantIndex(bubbles);
  const showsWorking =
    (!readOnly || fullSession) &&
    (starting || computeIsTurnActive(sessionStatus, state.status === "streaming"));
  // Native interruption needs an observed turn; local streaming can start before one exists.
  const interruptReady =
    !usesNativeSideChatFork(sessionHarness) || activeResponse?.state === "streaming";

  // Keep the newest content in view. A side chat is short and non-virtualized,
  // so a bottom sentinel scrolled on each change is enough.
  const transcriptRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const followTail = useRef(true);
  useEffect(() => {
    if (!fullSession || followTail.current) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [bubbles.length, activeResponse, fullSession]);

  const loadFailed = !pending && conversationLoadError !== null && bubbles.length === 0;
  const isEmpty = bubbles.length === 0 && !loadingConversation && !loadFailed && !showsWorking;

  const startSideChat = async (text: string) => {
    if (!onStart) return;
    // Keep the in-flight tab locked across tab switches and drawer remounts.
    useChatStore.getState().updateSideChatComposer(childId, (current) => ({
      ...current,
      starting: true,
    }));
    try {
      await onStart(text);
      // The quoted selection now travels with the fork's first message.
      useChatStore.getState().clearSideChatDraft(childId);
    } catch {
      // Re-enable the composer while preserving the draft for retry.
      useChatStore.getState().updateSideChatComposer(childId, (current) => ({
        ...current,
        starting: false,
      }));
    }
  };

  return (
    <ConversationScopeContext.Provider value={childId}>
      <div className="side-chat-backdrop flex h-full min-h-0 flex-col">
        <div
          ref={transcriptRef}
          className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 py-4"
          onScroll={(event) => {
            const node = event.currentTarget;
            followTail.current = node.scrollHeight - node.scrollTop - node.clientHeight < 64;
          }}
        >
          {fullSession && state.hasMoreHistory && (
            <Button
              variant="ghost"
              size="sm"
              disabled={state.loadingMoreHistory}
              onClick={() => {
                followTail.current = false;
                void useChatStore.getState().loadMoreHistory(childId);
              }}
            >
              {state.loadingMoreHistory ? "Loading…" : "Load earlier messages"}
            </Button>
          )}
          {loadFailed ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
              <TriangleAlertIcon className="size-6 text-muted-foreground" />
              <p className="text-ui font-medium text-foreground">
                {fullSession ? "Couldn’t load this session" : "Couldn’t load this side chat"}
              </p>
              <p className="max-w-[36ch] text-sm text-muted-foreground">
                {conversationLoadError?.message ?? "Please try again."}
              </p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="mt-1"
                onClick={() => void ensureConversationStreamed(childId)}
              >
                Retry
              </Button>
            </div>
          ) : fullSession && loadingConversation && bubbles.length === 0 ? (
            <p role="status" className="m-auto text-sm text-muted-foreground">
              Loading session…
            </p>
          ) : isEmpty ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
              <MessagesSquareIcon className="size-6 text-muted-foreground" />
              <p className="text-ui font-medium text-foreground">
                {fullSession ? "No messages yet" : "Side chat"}
              </p>
              {!fullSession && (
                <p className="max-w-[36ch] text-sm text-muted-foreground">{EMPTY_STATE_BODY}</p>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              {bubbles.map((bubble, index) => (
                <BubbleView
                  key={bubbleKey(bubble)}
                  bubble={bubble}
                  isLastAssistant={index === lastAssistantIndex}
                  showsWorking={showsWorking}
                  recoveryDisabled={readOnly}
                />
              ))}
              {shouldShowWorkingIndicator(showsWorking, bubbles) && <WorkingIndicator />}
              <div ref={bottomRef} />
            </div>
          )}
        </div>
        {!readOnly && !starting && (
          <SelectionPopup
            containerRef={transcriptRef}
            onReply={(text) => {
              useChatStore.getState().updateSideChatComposer(childId, (current) => ({
                ...current,
                ...appendReplyQuote(current, text),
              }));
            }}
          />
        )}
        {(!fullSession || !readOnly) && (
          <div className="shrink-0 px-3 pt-3 pb-5">
            {readOnly ? (
              <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-center text-sm text-muted-foreground">
                This side chat has ended and can’t be continued.
              </p>
            ) : (
              <SideChatComposer
                fullSession={fullSession}
                childId={childId}
                harness={sessionHarness}
                session={session}
                settingsSession={pending ? draftSource : session}
                agentId={boundAgentId}
                responseId={activeResponse?.responseId}
                interruptReady={interruptReady}
                busy={showsWorking}
                pending={pending}
                starting={starting}
                onStart={onStart ? startSideChat : undefined}
              />
            )}
          </div>
        )}
      </div>
    </ConversationScopeContext.Provider>
  );
}

/**
 * The side chat's composer. On a live tab it sends to the child via
 * `pinnedConversationId` (so turns land on the side thread, not the main one)
 * with the main composer's affordances (attach, dictation). On a pending tab it
 * lets the user draft text and choose settings before `onStart` creates the fork.
 * The draft is kept if creation fails.
 */
function SideChatComposer({
  fullSession,
  childId,
  harness,
  session,
  settingsSession,
  agentId,
  responseId,
  interruptReady,
  busy,
  pending,
  starting,
  onStart,
}: {
  fullSession: boolean;
  childId: string;
  harness: string | null | undefined;
  session: Session | null | undefined;
  settingsSession: Session | null | undefined;
  agentId: string | null;
  responseId: string | undefined;
  interruptReady: boolean;
  busy: boolean;
  pending: boolean;
  starting: boolean;
  onStart?: (text: string) => Promise<void>;
}) {
  const send = useChatStore((s) => s.send);
  const sendSlashCommand = useChatStore((s) => s.sendSlashCommand);
  const queryClient = useQueryClient();
  const settingsSaving = useIsMutating({ mutationKey: sideChatSettingsMutationKey(childId) }) > 0;
  const settingsSavePending = () =>
    queryClient.isMutating({ mutationKey: sideChatSettingsMutationKey(childId) }) > 0;
  const clearSideChatDraft = useChatStore((s) => s.clearSideChatDraft);
  // Unsent text + attachments live in the store, keyed by child id, NOT in
  // component state: this pane mounts in the desktop rail or the mobile
  // drawer's portal, so crossing the `md` breakpoint (a phone rotating) moves
  // it between subtrees and unmounts it, and so does switching rail tabs.
  const composer = useChatStore((s) => s.sideChatComposers[childId]);
  const { text, files } = composer ?? EMPTY_SIDE_CHAT_COMPOSER;
  const updateComposer = useChatStore((s) => s.updateSideChatComposer);
  const clearComposer = useChatStore((s) => s.clearSideChatComposer);
  const enqueueMessage = useChatStore((s) => s.enqueueMessage);
  const maybeFlushQueuedHead = useChatStore((s) => s.maybeFlushQueuedHead);
  const dequeueMessage = useChatStore((s) => s.dequeueMessage);
  const steerMessage = useChatStore((s) => s.steerMessage);
  const steerAllQueuedMessages = useChatStore((s) => s.steerAllQueuedMessages);
  const reorderQueuedMessage = useChatStore((s) => s.reorderQueuedMessage);
  const allQueuedMessages = useChatStore((s) => s.queuedMessages);
  const queuedMessages = useMemo(
    () => allQueuedMessages.filter((message) => message.conversationId === childId),
    [allQueuedMessages, childId],
  );
  // Grid sessions keep waiting for the turn to end; side chats queue like the main composer.
  const queues = !pending && !fullSession;
  useEffect(() => {
    if (queues && queuedMessages.length > 0 && !settingsSaving) maybeFlushQueuedHead(childId);
  }, [queues, queuedMessages, busy, agentId, childId, maybeFlushQueuedHead, settingsSaving]);
  const info = useServerInfo();
  const attachmentPolicy = info === "loading" ? null : (info.filesystem_attachments ?? null);
  useEffect(() => {
    if (info === "loading") return;
    const current = useChatStore.getState().sideChatComposers[childId];
    if (!current) return;
    const { accepted, errors } = validateAttachments(current.files, attachmentPolicy);
    if (errors.length === 0) return;
    updateComposer(childId, (draft) => ({ ...draft, files: accepted }));
    errors.forEach((message) => toast.error(message));
  }, [attachmentPolicy, childId, info, updateComposer]);
  const { failedSendDraft, restoredSendDraft } = useConversationEntryState(
    fullSession ? childId : null,
  );
  useEffect(() => {
    if (!fullSession || !failedSendDraft) return;
    const current = useChatStore.getState().sideChatComposers[childId] ?? EMPTY_SIDE_CHAT_COMPOSER;
    const failed = takeFailedSendDraft(
      childId,
      !!(current.text.trim() || current.files.length || current.quotes?.length),
    );
    if (!failed) return;
    const { accepted, errors } = validateAttachments(failed.files, attachmentPolicy);
    errors.forEach((message) => toast.error(message));
    updateComposer(childId, () => ({ text: failed.text, files: accepted }));
    // Track the displayed draft for delivery; filtered files require a fresh send ID.
    const entry = conversationRegistry.peek(childId)!;
    const restored = entry.getState().restoredSendDraft;
    if (restored)
      entry.setState({
        restoredSendDraft: { ...restored, files: accepted, replyDraft: undefined },
        pendingRetryStableId: errors.length > 0 ? null : entry.getState().pendingRetryStableId,
      });
  }, [fullSession, childId, failedSendDraft, updateComposer, attachmentPolicy]);
  useEffect(() => {
    if (!fullSession || !restoredSendDraft?.delivered) return;
    const entry = conversationRegistry.peek(childId);
    const restored = entry?.getState().restoredSendDraft;
    if (!restored?.delivered || restored.conversationId !== childId) return;
    const current = useChatStore.getState().sideChatComposers[childId] ?? EMPTY_SIDE_CHAT_COMPOSER;
    entry!.setState({ restoredSendDraft: null });
    if (current.quotes?.length) return;
    if (
      restoredSendDraftUnchanged(
        restored,
        restored.stableId,
        current.text,
        current.files,
        undefined,
      )
    )
      clearComposer(childId);
  }, [fullSession, childId, restoredSendDraft, clearComposer]);
  const setText = useCallback(
    (next: string) => updateComposer(childId, (current) => ({ ...current, text: next })),
    [childId, updateComposer],
  );
  const [activeTextId, setActiveTextId] = useState<string | null>(null);
  const editText = useCallback(
    (id: string | null, next: string) =>
      updateComposer(childId, (current) =>
        id === null
          ? { ...current, text: next }
          : {
              ...current,
              quotes: current.quotes?.map((quote) =>
                quote.id === id ? { ...quote, before: next } : quote,
              ),
            },
      ),
    [childId, updateComposer],
  );
  const setFiles = useCallback(
    (mutate: (current: File[]) => File[]) =>
      updateComposer(childId, (current) => ({ ...current, files: mutate(current.files) })),
    [childId, updateComposer],
  );
  const [interrupting, setInterrupting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const tailTextareaRef = useRef<HTMLTextAreaElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const voiceEditsRef = useRef(
    new Map<string | null, { before: string; after: string; quoteIds: Set<string> }>(),
  );
  const reconcileVoiceEdits = useCallback((current: SideChatComposerDraft) => {
    for (const [id, edit] of voiceEditsRef.current) {
      // Appending a quote moves the old tail into that quote's preceding text.
      const targetId =
        id ?? current.quotes?.find((item) => !edit.quoteIds.has(item.id))?.id ?? null;
      const value =
        targetId === null
          ? current.text
          : current.quotes?.find((item) => item.id === targetId)?.before;
      if (value !== edit.after || targetId !== id) voiceEditsRef.current.delete(id);
      if (value === edit.after && targetId !== id) voiceEditsRef.current.set(targetId, edit);
    }
  }, []);
  // Manual edits and merged reply fields relinquish dictation's undo ownership.
  useLayoutEffect(() => {
    reconcileVoiceEdits(composer ?? EMPTY_SIDE_CHAT_COMPOSER);
  }, [composer, reconcileVoiceEdits]);
  const activeText = composer?.quotes?.find((quote) => quote.id === activeTextId)?.before ?? text;
  const setActiveText = useCallback(
    (next: string) => {
      const current =
        useChatStore.getState().sideChatComposers[childId] ?? EMPTY_SIDE_CHAT_COMPOSER;
      reconcileVoiceEdits(current);
      const before =
        activeTextId === null
          ? current.text
          : current.quotes?.find((item) => item.id === activeTextId)?.before;
      if (before === undefined) return;
      const previous = voiceEditsRef.current.get(activeTextId);
      voiceEditsRef.current.set(activeTextId, {
        before: previous?.before ?? before,
        after: next,
        quoteIds: previous?.quoteIds ?? new Set(current.quotes?.map((item) => item.id)),
      });
      editText(activeTextId, next);
    },
    [activeTextId, childId, editText, reconcileVoiceEdits],
  );
  const dictation = useDictationInsert(activeText, setActiveText, textareaRef);

  // This tab's seeded text: on a pending tab the "Ask in side chat" selection
  // to QUOTE, on a live tab the `/side` question to SEND.
  const draft = useChatStore((s) => s.sideChatDrafts[childId]);
  const quote = pending ? draft : undefined;
  const quotes = [
    ...(quote === undefined ? [] : [{ id: "seed", before: "", text: quote }]),
    ...(composer?.quotes ?? []),
  ];
  useAutoGrowTextarea(tailTextareaRef, text, quotes.length ? Infinity : 10);
  const autoSend = pending ? undefined : draft;
  useEffect(() => {
    setActiveTextId(null);
    textareaRef.current = tailTextareaRef.current;
    if (quote !== undefined || composer?.quotes?.length) tailTextareaRef.current?.focus();
  }, [quote, composer?.quotes?.length]);
  // Re-read the labels so a side chat the server just sealed turns read-only.
  const refreshLabels = useCallback(
    () => void queryClient.invalidateQueries({ queryKey: ["session", childId] }),
    [queryClient, childId],
  );
  // Send the seeded question once the child's agent binding is known. The text
  // stays in the store until this dispatches — never copied into component
  // state first — so unmounting in the meantime (closing the mobile drawer,
  // switching tabs) defers the send instead of discarding the only copy.
  useEffect(() => {
    if (autoSend === undefined || agentId === null || settingsSaving) return;
    // Re-read and consume the LIVE draft rather than the one captured at
    // render: a replayed mount effect (React StrictMode in development) would
    // otherwise send the captured question a second time.
    const question = useChatStore.getState().sideChatDrafts[childId];
    if (question === undefined) return;
    clearSideChatDraft(childId);
    void send(question, agentId, undefined, { pinnedConversationId: childId }).finally(
      refreshLabels,
    );
  }, [autoSend, agentId, send, childId, clearSideChatDraft, refreshLabels, settingsSaving]);

  const ready = (pending ? !starting : agentId !== null) && !settingsSaving;
  const canSend =
    serializeReplyDraft({ quotes, text }).trim().length > 0 || (!pending && files.length > 0);
  const addFiles = (incoming: File[]) => {
    const { accepted, errors } = validateAttachments(incoming, attachmentPolicy);
    errors.forEach((message) => toast.error(message));
    if (accepted.length) setFiles((current) => [...current, ...accepted]);
  };
  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = clipboardFiles(event.clipboardData);
    if (!pasted.length) return;
    event.preventDefault();
    if (!ready) return;
    if (pending) {
      toast.error("Send a text question to start the side chat before attaching files.");
      return;
    }
    addFiles(pasted);
  };
  // Slash suggestions mirror the main composer: the session's skills plus /help.
  // Other built-ins (/model, /compact, …) act on the main session and stay out.
  const isMobile = useIsMobileViewport();
  const [inputFocused, setInputFocused] = useState(false);
  const [commandNotice, setCommandNotice] = useState<string | null>(null);
  const nativeSession = isNativeTerminalSession({ harness, labels: session?.labels });
  // A generic side chat has no host/workspace of its own: it forks the parent and
  // runs in the parent's workspace. Discover skills against the parent (the same
  // catalog the main composer shows); the hostless child always reads unavailable.
  const parentId = fullSession
    ? childId
    : nativeSession
      ? null
      : parentIdFromLabels(session?.labels);
  const { session: parentSession, isLoading: parentLoading } = useSession(parentId);
  const {
    skills,
    skillsStatus,
    refetch: refreshSkills,
  } = useSkills({
    target:
      !pending && parentId !== null && parentSession?.hostId && parentSession.workspace
        ? {
            sessionId: parentId,
            scope: {
              hostId: parentSession.hostId,
              harness: parentSession.harness,
              workspace: parentSession.workspace,
              agentId: parentSession.agentId,
              subAgentName: parentSession.subAgentName,
            },
          }
        : null,
    enabled: !pending && ready,
    // A still-loading parent lookup has no target yet; treat it as discovery in
    // flight so a quickly typed /skill is held, not sent to the agent as plaintext.
    starting: starting || (parentId !== null && parentLoading),
  });
  const skillPrefix = skillInvocationPrefix(harness);
  const sideChatBuiltins = useMemo<Record<string, string>>(() => {
    const builtins: Record<string, string> = {};
    if (!nativeSession) builtins["/help"] = BUILTIN_SLASH_COMMANDS["/help"];
    return builtins;
  }, [nativeSession]);
  // A skill whose command exactly matches a built-in can never run (the built-in
  // wins), so drop it to keep the menu's labels aligned with what submitting runs.
  const routableSkills = useMemo(
    () => skills.filter((skill) => !Object.hasOwn(sideChatBuiltins, `${skillPrefix}${skill.name}`)),
    [skills, skillPrefix, sideChatBuiltins],
  );
  const skillCommands = useMemo(
    () =>
      Object.fromEntries(
        routableSkills.map((skill) => [`${skillPrefix}${skill.name}`, skillMenuDescription(skill)]),
      ),
    [routableSkills, skillPrefix],
  );
  const skillLabels = useMemo(
    () => skillDisplayNames(routableSkills, skillPrefix),
    [routableSkills, skillPrefix],
  );
  const menuCommands = useMemo(
    () => ({ ...sideChatBuiltins, ...skillCommands }),
    [sideChatBuiltins, skillCommands],
  );
  // /help lists what this side chat offers; shown locally, never sent as a turn.
  const showHelp = useCallback(() => {
    const lines = Object.entries(menuCommands).map(([name, desc]) => `${name} — ${desc}`);
    setCommandNotice(lines.join("\n"));
  }, [menuCommands]);
  // Tab fills the draft with the chosen command; Enter or click runs a built-in.
  const completeMenuSelection = (cmd: string) => {
    setText(slashCompletion.complete(cmd).text);
  };
  // Insert a chosen skill at the caret; a built-in runs immediately (no args).
  const applyMenuSelection = (cmd: string) => {
    if (Object.hasOwn(sideChatBuiltins, cmd)) {
      setText("");
      if (cmd === "/help") showHelp();
      return;
    }
    completeMenuSelection(cmd);
  };
  const slashCompletion = useSlashCompletion({
    text,
    commands: menuCommands,
    skills: skillCommands,
    labels: skillLabels,
    textareaRef,
    prefix: skillPrefix,
    status: skillsStatus,
    mobile: isMobile,
    mobileEnterCompletes: false,
    escapeClearsOnlyWithContent: true,
    // A pending tab has no session yet; a native TUI reads slash text itself.
    allowOpen:
      inputFocused && !pending && !nativeSession && quotes.length === 0 && files.length === 0,
    onSelect: applyMenuSelection,
    onTabComplete: completeMenuSelection,
    clearText: () => setText(""),
  });
  // Like the main composer, a draft turns Interrupt into Send.
  const showInterrupt = !pending && busy && (fullSession || !canSend);

  const interruptSideChat = () => {
    if (interrupting || !interruptReady) return;
    setInterrupting(true);
    void interrupt(childId, responseId)
      .catch(() => toast.error("Couldn’t interrupt this side chat. Please try again."))
      .finally(() => setInterrupting(false));
  };

  const submit = () => {
    if (settingsSavePending()) return;
    const trimmed = isSlashCommandText(text) ? text.trim() : text.trimEnd();
    if (pending) {
      if (!canSend || starting || !onStart) return;
      // Keep the text so a failed fork can be retried without re-typing.
      void onStart(serializeReplyDraft({ quotes, text: trimmed }));
      return;
    }
    if ((busy && fullSession) || !canSend || agentId === null) return;
    // On in-process harnesses /help and skills resolve locally; native TUIs
    // (claude-native, codex-native) read the text themselves, and a quote or
    // attachment is always an ordinary message.
    const routeLocally = !nativeSession && quotes.length === 0 && files.length === 0;
    // A lone command token with skill discovery in flight has nothing to run
    // yet; only hold when the submission would route locally.
    if (routeLocally && slashCompletion.pendingCompletion) return;
    const builtin = routeLocally
      ? matchSlashCommandInvocation(trimmed, Object.keys(sideChatBuiltins))
      : null;
    const skill = routeLocally
      ? matchSlashCommandInvocation(trimmed, Object.keys(skillCommands))
      : null;
    const runSkill = (match: { command: string; args: string }) => {
      clearComposer(childId);
      void sendSlashCommand(match.command.slice(1), match.args, agentId, {
        pinnedConversationId: childId,
      }).finally(refreshLabels);
    };
    // A multi-word catalog skill outranks a built-in that matches only its first
    // word ("/help desk ..." runs the skill); an exact-name collision keeps
    // built-in precedence.
    if (skill !== null && skill.command !== trimmed.split(/\s+/)[0]) {
      runSkill(skill);
      return;
    }
    if (builtin !== null) {
      setText("");
      if (builtin.command === "/help") showHelp();
      return;
    }
    if (skill !== null) {
      runSkill(skill);
      return;
    }
    const outgoing = files.length > 0 ? files : undefined;
    const message = serializeReplyDraft({ quotes, text: trimmed });
    const child = conversationRegistry.peek(childId)?.getState();
    if (
      queues &&
      child !== undefined &&
      shouldQueueSend(
        childId,
        child.status,
        child.sessionStatus,
        useChatStore.getState().queuedMessages,
        readAlwaysSteer(),
        false,
        holdsInitialDraft(child),
      )
    ) {
      clearComposer(childId);
      enqueueMessage(message, outgoing, snapshotReplyDraft({ quotes, text: trimmed }), childId);
      return;
    }
    // A running Codex chat refuses a steered /compact; keep the draft as the main composer does.
    if (isCompactControl(harness, message, outgoing) && rejectBusyCompact(childId)) return;
    clearComposer(childId);
    void send(message, agentId, outgoing, { pinnedConversationId: childId }).finally(refreshLabels);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>, intent: ComposerKeyIntent) => {
    // Mod+Enter submits any draft, then steers the whole queue into the running turn.
    if (queues && intent.shouldSteerAllFromKeyboard) {
      event.preventDefault();
      if (settingsSavePending()) return;
      submit();
      steerAllQueuedMessages(childId);
      return;
    }
    if (slashCompletion.handleKey(event, intent)) return;
    // A grid pane is a whole session, so only side chats let Escape stop the turn.
    if (event.key === "Escape" && queues && busy) {
      event.preventDefault();
      interruptSideChat();
      return;
    }
    if (intent.shouldSubmitFromKeyboard) {
      event.preventDefault();
      submit();
    }
  };
  const focusText = (id: string | null, input: HTMLTextAreaElement) => {
    setActiveTextId(id);
    textareaRef.current = input;
    dictation.noteFocus();
  };

  return (
    <>
      {!pending && (
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={attachmentAccept(attachmentPolicy)}
          className="hidden"
          onChange={(event) => {
            if (event.target.files) {
              addFiles(Array.from(event.target.files));
              event.target.value = "";
            }
          }}
        />
      )}
      {commandNotice !== null && (
        <ComposerFeedbackRow data-testid="side-chat-command-notice">
          {commandNotice}
        </ComposerFeedbackRow>
      )}
      {queues && (
        <TooltipProvider>
          <QueuedMessagesStrip
            messages={queuedMessages}
            onDelete={dequeueMessage}
            onEdit={(queueId) => {
              const target = queuedMessages.find((message) => message.queueId === queueId);
              if (!target) return;
              const restored = restoreReplyDraft(target.text, target.replyDraft);
              updateComposer(childId, () => ({ ...restored, files: target.files ?? [] }));
              dequeueMessage(queueId);
              tailTextareaRef.current?.focus();
            }}
            onSteer={
              settingsSaving
                ? undefined
                : (queueId) => {
                    if (!settingsSavePending()) steerMessage(queueId);
                  }
            }
            onReorder={reorderQueuedMessage}
            widthClassName="mx-3 w-auto"
          />
        </TooltipProvider>
      )}
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{
          ref: tailTextareaRef,
          value: text,
          onChange: (event) => {
            if (commandNotice !== null) setCommandNotice(null);
            slashCompletion.onSelectionChange(event.target);
            setText(event.target.value);
          },
          onSelect: (event) => slashCompletion.onSelectionChange(event.currentTarget),
          onFocus: (event) => {
            setInputFocused(true);
            focusText(null, event.currentTarget);
          },
          // Menu rows preventDefault on mousedown so selecting one keeps focus.
          onBlur: () => setInputFocused(false),
          placeholder: fullSession ? "Reply to this session..." : "Ask a side question...",
          disabled: !ready,
          "data-testid": "side-chat-input",
          onKeyDown: handleKeyDown,
          onPaste,
          className: quotes.length ? "max-h-none overflow-y-hidden" : undefined,
        }}
        slots={{
          beforeInput: slashCompletion.open ? (
            <SlashCommandMenu
              query={slashCompletion.query}
              activeIndex={slashCompletion.index}
              onSelect={applyMenuSelection}
              commands={slashCompletion.commands}
              builtinNames={slashCompletion.builtinNames}
              labels={slashCompletion.labels}
              skillsStatus={skillsStatus}
              onRetrySkills={() => void refreshSkills()}
            />
          ) : undefined,
          inputPrefix:
            quotes.length === 0 ? undefined : (
              <ReplyDraftBlocks
                quotes={quotes}
                activeTextId={activeTextId}
                keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
                disabled={!ready}
                inputFor={(replyQuote) => ({
                  onChange: (event) => editText(replyQuote.id, event.target.value),
                  onFocus: (event) => focusText(replyQuote.id, event.currentTarget),
                  onKeyDown: handleKeyDown,
                  onPaste,
                })}
                onRemove={(id) => {
                  if (id === "seed") clearSideChatDraft(childId);
                  else
                    updateComposer(childId, (current) => ({
                      ...current,
                      ...removeReplyQuote({ ...current, quotes: current.quotes ?? [] }, id),
                    }));
                }}
              />
            ),
          attachments:
            !pending && files.length > 0 ? (
              <ComposerAttachments
                files={files}
                onRemove={(index) => setFiles((prev) => prev.filter((_, i) => i !== index))}
              />
            ) : undefined,
        }}
        actions={{
          leading: pending ? null : (
            <ComposerAddMenu
              disabled={agentId === null}
              onAttach={() => fileInputRef.current?.click()}
              showGoal={false}
              showPlan={false}
              planActive={false}
              testIdPrefix="side-chat"
            />
          ),
          trailing: (
            <>
              {!fullSession && settingsSession?.id && (
                <SideChatSettings
                  key={childId}
                  session={settingsSession}
                  draft={
                    pending
                      ? {
                          settings: composer?.settings,
                          disabled: starting,
                          onChange: (updates) =>
                            updateComposer(childId, (current) => ({
                              ...current,
                              settings: { ...current.settings, ...updates },
                            })),
                        }
                      : undefined
                  }
                />
              )}
              <ComposerMicButton
                className="size-8 md:size-7"
                disabled={!ready}
                onVoiceStart={() => voiceEditsRef.current.clear()}
                onVoiceDiscard={() => {
                  reconcileVoiceEdits(
                    useChatStore.getState().sideChatComposers[childId] ?? EMPTY_SIDE_CHAT_COMPOSER,
                  );
                  const edits = [...voiceEditsRef.current];
                  voiceEditsRef.current.clear();
                  for (const [id, edit] of edits) editText(id, edit.before);
                }}
                onTranscript={(spoken) => dictation.appendFinal(spoken)}
                onInterim={(spoken) => dictation.replaceInterim(spoken)}
              />
              <ComposerSendButton
                type="button"
                label={
                  fullSession
                    ? showInterrupt
                      ? "Interrupt session"
                      : "Send message"
                    : showInterrupt
                      ? "Interrupt side chat"
                      : "Send side question"
                }
                interrupt={showInterrupt}
                busy={showInterrupt && interrupting}
                disabled={showInterrupt ? interrupting || !interruptReady : !canSend || !ready}
                onClick={showInterrupt ? interruptSideChat : submit}
                data-testid={showInterrupt ? "side-chat-interrupt" : "side-chat-send"}
              />
            </>
          ),
        }}
      />
    </>
  );
}
