import { SelectionPopup } from "./SelectionPopup";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { MessagesSquareIcon, TriangleAlertIcon } from "lucide-react";
import { toast } from "sonner";
import { useQueryClient } from "@tanstack/react-query";
import { getCurrentAuthorId } from "@/lib/identity";
import { attachmentAccept, validateAttachments } from "@/lib/attachments";
import { useServerInfo } from "@/lib/CapabilitiesContext";
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
import { ChatComposer, ComposerSendButton } from "@/components/composer/ChatComposer";
import { ComposerAddMenu } from "@/components/composer/ComposerAddMenu";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { ComposerAttachments } from "@/components/ComposerAttachments";
import { ReplyDraftBlocks } from "@/components/composer/ReplyDraftBlocks";
import { Button } from "@/components/ui/button";
import {
  EMPTY_SIDE_CHAT_COMPOSER,
  ensureConversationStreamed,
  useChatStore,
} from "@/store/chatStore";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useDictationInsert } from "@/hooks/useDictationInsert";
import { useSession } from "@/hooks/useSession";
import { usesNativeSideChatFork } from "@/lib/sideChat";
import { serializeReplyDraft } from "@/lib/replyDraft";
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
}: {
  childId: string;
  selectionParentId?: string;
  onStart?: (text: string) => Promise<void>;
  /** A dead, restored Codex side chat: show the transcript without a composer. */
  readOnly?: boolean;
}) {
  const pending = isPendingSideChat(childId);
  const starting = useChatStore((s) => s.sideChatComposers[childId]?.starting ?? false);
  // The server seals a side chat whose fork died with its runner.
  const { session } = useSession(pending ? null : childId);
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
  const filterHistory = !usesNativeSideChatFork(sessionHarness);
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
    !readOnly && (starting || computeIsTurnActive(sessionStatus, state.status === "streaming"));
  // Native interruption needs an observed turn; local streaming can start before one exists.
  const interruptReady =
    !usesNativeSideChatFork(sessionHarness) || activeResponse?.state === "streaming";

  // Keep the newest content in view. A side chat is short and non-virtualized,
  // so a bottom sentinel scrolled on each change is enough.
  const transcriptRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [bubbles.length, activeResponse]);

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
        <div ref={transcriptRef} className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 py-4">
          {loadFailed ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
              <TriangleAlertIcon className="size-6 text-muted-foreground" />
              <p className="text-ui font-medium text-foreground">Couldn’t load this side chat</p>
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
          ) : isEmpty ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
              <MessagesSquareIcon className="size-6 text-muted-foreground" />
              <p className="text-ui font-medium text-foreground">Side chat</p>
              <p className="max-w-[36ch] text-sm text-muted-foreground">{EMPTY_STATE_BODY}</p>
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
                quotes: [...(current.quotes ?? []), text],
              }));
            }}
          />
        )}
        <div className="shrink-0 px-3 pt-3 pb-5">
          {readOnly ? (
            <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-center text-sm text-muted-foreground">
              This side chat has ended and can’t be continued.
            </p>
          ) : (
            <SideChatComposer
              childId={childId}
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
      </div>
    </ConversationScopeContext.Provider>
  );
}

/**
 * The side chat's composer. On a live tab it sends to the child via
 * `pinnedConversationId` (so turns land on the side thread, not the main one)
 * with the main composer's affordances (attach, dictation). On a pending tab it
 * has just text + send: the first message creates the fork via `onStart`, and
 * the text is kept if creation fails so it isn't lost.
 */
function SideChatComposer({
  childId,
  agentId,
  responseId,
  interruptReady,
  busy,
  pending,
  starting,
  onStart,
}: {
  childId: string;
  agentId: string | null;
  responseId: string | undefined;
  interruptReady: boolean;
  busy: boolean;
  pending: boolean;
  starting: boolean;
  onStart?: (text: string) => Promise<void>;
}) {
  const send = useChatStore((s) => s.send);
  const queryClient = useQueryClient();
  const clearSideChatDraft = useChatStore((s) => s.clearSideChatDraft);
  // Unsent text + attachments live in the store, keyed by child id, NOT in
  // component state: this pane mounts in the desktop rail or the mobile
  // drawer's portal, so crossing the `md` breakpoint (a phone rotating) moves
  // it between subtrees and unmounts it, and so does switching rail tabs.
  const composer = useChatStore((s) => s.sideChatComposers[childId]);
  const { text, files } = composer ?? EMPTY_SIDE_CHAT_COMPOSER;
  const updateComposer = useChatStore((s) => s.updateSideChatComposer);
  const clearComposer = useChatStore((s) => s.clearSideChatComposer);
  const setText = useCallback(
    (next: string) => updateComposer(childId, (current) => ({ ...current, text: next })),
    [childId, updateComposer],
  );
  const setFiles = useCallback(
    (mutate: (current: File[]) => File[]) =>
      updateComposer(childId, (current) => ({ ...current, files: mutate(current.files) })),
    [childId, updateComposer],
  );
  // Configurable attachment policy from server; use it to validate and set accept attribute
  const info = useServerInfo();
  const policy = info === "loading" ? undefined : info.filesystem_attachment_policy;
  const accept = attachmentAccept(policy);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [interrupting, setInterrupting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const voiceSnapshotRef = useRef("");
  const dictation = useDictationInsert(text, setText, textareaRef);
  useAutoGrowTextarea(textareaRef, text);

  // This tab's seeded text: on a pending tab the "Ask in side chat" selection
  // to QUOTE, on a live tab the `/side` question to SEND.
  const draft = useChatStore((s) => s.sideChatDrafts[childId]);
  const quote = pending ? draft : undefined;
  const quotes = [
    ...(quote === undefined ? [] : [{ id: "seed", before: "", text: quote }]),
    ...(composer?.quotes ?? []).map((selection, index) => ({
      id: String(index),
      before: "",
      text: selection,
    })),
  ];
  const autoSend = pending ? undefined : draft;
  useEffect(() => {
    if (quote !== undefined || composer?.quotes?.length) textareaRef.current?.focus();
  }, [quote, composer?.quotes]);
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
    if (autoSend === undefined || agentId === null) return;
    // Re-read and consume the LIVE draft rather than the one captured at
    // render: a replayed mount effect (React StrictMode in development) would
    // otherwise send the captured question a second time.
    const question = useChatStore.getState().sideChatDrafts[childId];
    if (question === undefined) return;
    clearSideChatDraft(childId);
    void send(question, agentId, undefined, { pinnedConversationId: childId }).finally(
      refreshLabels,
    );
  }, [autoSend, agentId, send, childId, clearSideChatDraft, refreshLabels]);

  const ready = pending ? !starting : agentId !== null;
  const canSend = text.trim().length > 0 || (!pending && files.length > 0);
  const showInterrupt = !pending && busy;

  const interruptSideChat = () => {
    if (interrupting || !interruptReady) return;
    setInterrupting(true);
    void interrupt(childId, responseId)
      .catch(() => toast.error("Couldn’t interrupt this side chat. Please try again."))
      .finally(() => setInterrupting(false));
  };

  const submit = () => {
    const trimmed = text.trim();
    if (pending) {
      if (trimmed.length === 0 || starting || !onStart) return;
      // Keep the text so a failed fork can be retried without re-typing.
      void onStart(serializeReplyDraft({ quotes, text: trimmed }));
      return;
    }
    if (busy || (trimmed.length === 0 && files.length === 0) || agentId === null) return;
    setSendError(null);
    setAttachmentError(null);
    const outgoing = files;
    const outgoingQuotes = composer?.quotes ?? [];
    clearComposer(childId);
    void send(
      serializeReplyDraft({ quotes, text: trimmed }),
      agentId,
      outgoing.length > 0 ? outgoing : undefined,
      {
        pinnedConversationId: childId,
        onError: (message) => {
          // Restore the trimmed text, quotes and outgoing files to the store on send error
          updateComposer(childId, (current) => ({
            ...current,
            text: current.text ? `${trimmed}\n\n${current.text}` : trimmed,
            quotes: [...outgoingQuotes, ...(current.quotes ?? [])],
            files: [...outgoing, ...current.files],
          }));
          setSendError(message);
        },
      },
    ).finally(refreshLabels);
  };

  return (
    <>
      {!pending && (
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={accept}
          className="hidden"
          onChange={(event) => {
            if (event.target.files) {
              const incoming = Array.from(event.target.files);
              const { accepted, errors } = validateAttachments(incoming, policy, files);
              if (accepted.length > 0) {
                setFiles((current) => [...current, ...accepted]);
              }
              setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
              event.target.value = "";
            }
          }}
        />
      )}
      {(attachmentError || sendError) && (
        <p role="alert" className="mb-2 text-sm text-destructive">
          {attachmentError || sendError}
        </p>
      )}
      <ChatComposer
        keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
        input={{
          ref: textareaRef,
          value: text,
          onChange: (event) => setText(event.target.value),
          placeholder: "Ask a side question...",
          disabled: !ready,
          "data-testid": "side-chat-input",
          onPaste: pending
            ? undefined
            : (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
                // Attach file-kind clipboard items instead of letting them insert as text
                const items = e.clipboardData?.items;
                if (!items) return;
                const files_from_paste: File[] = [];
                for (let i = 0; i < items.length; i++) {
                  const item = items[i];
                  if (item.kind === "file") {
                    const file = item.getAsFile();
                    if (file) files_from_paste.push(file);
                  }
                }
                if (files_from_paste.length > 0) {
                  const { accepted, errors } = validateAttachments(files_from_paste, policy, files);
                  if (accepted.length > 0) {
                    setFiles((current) => [...current, ...accepted]);
                    e.preventDefault();
                  }
                  setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
                }
              },
          onKeyDown: (event, intent) => {
            if (intent.shouldSubmitFromKeyboard) {
              event.preventDefault();
              submit();
            }
          },
        }}
        slots={{
          inputPrefix:
            quotes.length === 0 ? undefined : (
              <ReplyDraftBlocks
                quotes={quotes}
                activeTextId={null}
                showTextInputs={false}
                keyboard={{ submitWithModEnter: false, preventsKeyboardSubmit: false }}
                disabled={!ready}
                inputFor={() => ({})}
                onRemove={(id) => {
                  if (id === "seed") clearSideChatDraft(childId);
                  else
                    updateComposer(childId, (current) => ({
                      ...current,
                      quotes: current.quotes?.filter((_, index) => String(index) !== id),
                    }));
                }}
              />
            ),
          attachments:
            !pending && files.length > 0 ? (
              <ComposerAttachments
                files={files}
                onRemove={(index) => {
                  setFiles((current) => current.filter((_, i) => i !== index));
                  setAttachmentError(null);
                }}
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
              <ComposerMicButton
                className="size-8 md:size-7"
                disabled={!ready}
                onVoiceStart={() => {
                  voiceSnapshotRef.current = text;
                }}
                onVoiceDiscard={() => setText(voiceSnapshotRef.current)}
                onTranscript={(spoken) => dictation.appendFinal(spoken)}
                onInterim={(spoken) => dictation.replaceInterim(spoken)}
              />
              <ComposerSendButton
                type="button"
                label={showInterrupt ? "Interrupt side chat" : "Send side question"}
                interrupt={showInterrupt}
                busy={interrupting}
                disabled={
                  showInterrupt ? interrupting || !interruptReady : !canSend || !ready || busy
                }
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
