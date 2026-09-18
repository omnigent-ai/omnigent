import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpIcon, MessagesSquareIcon, TriangleAlertIcon } from "lucide-react";
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
  mergePendingBubbles,
  reorderCommittedRequestElicitations,
  shouldShowWorkingIndicator,
  stripGatedSubagentRoutingChips,
} from "@/components/chat/chatBubbleParts";
import { ChatComposer } from "@/components/composer/ChatComposer";
import { ComposerAddMenu } from "@/components/composer/ComposerAddMenu";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { ComposerAttachments } from "@/components/ComposerAttachments";
import { Button } from "@/components/ui/button";
import { useChatStore, ensureConversationStreamed } from "@/store/chatStore";
import { useConversationEntryState } from "@/hooks/useConversationEntryState";
import { useDictationInsert } from "@/hooks/useDictationInsert";
import { usesNativeSideChatFork } from "@/lib/sideChat";
import { stopSession } from "@/lib/sessionsApi";
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

// Read-only (dead, restored) Codex side chats we've already stopped this
// session, so re-selecting the tab doesn't re-fire stop_session each time.
const killedSideChats = new Set<string>();

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
  onStart,
  readOnly = false,
}: {
  childId: string;
  onStart?: (text: string) => Promise<void>;
  /** A dead, restored Codex side chat: show the transcript but no composer, and
   *  stop its session. Defaults to false (a live, sendable side chat). */
  readOnly?: boolean;
}) {
  const pending = isPendingSideChat(childId);
  // Open the child's stream once (real tabs only) so it hydrates and streams
  // here. The store guards a double-bind and re-binds a failed entry, so
  // re-mounts / tab switches / retries are cheap.
  useEffect(() => {
    if (!pending) void ensureConversationStreamed(childId);
  }, [pending, childId]);
  // A restored, read-only Codex side chat is a dead ephemeral fork; stop its
  // session once (best-effort) so nothing lingers server-side.
  useEffect(() => {
    if (readOnly && !pending && !killedSideChats.has(childId)) {
      killedSideChats.add(childId);
      void stopSession(childId).catch(() => {});
    }
  }, [readOnly, pending, childId]);

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
  const showsWorking = computeIsWorking(sessionStatus);

  // Keep the newest content in view. A side chat is short and non-virtualized,
  // so a bottom sentinel scrolled on each change is enough.
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [bubbles.length, activeResponse]);

  const loadFailed = !pending && conversationLoadError !== null && bubbles.length === 0;
  const isEmpty = bubbles.length === 0 && !loadingConversation && !loadFailed;

  return (
    <ConversationScopeContext.Provider value={pending ? null : childId}>
      <div className="side-chat-backdrop flex h-full min-h-0 flex-col">
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 py-4">
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
                />
              ))}
              {shouldShowWorkingIndicator(showsWorking, bubbles) && <WorkingIndicator />}
              <div ref={bottomRef} />
            </div>
          )}
        </div>
        <div className="shrink-0 p-3">
          {readOnly ? (
            <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-center text-sm text-muted-foreground">
              This side chat has ended and can’t be continued.
            </p>
          ) : (
            <SideChatComposer
              childId={childId}
              agentId={boundAgentId}
              busy={showsWorking}
              pending={pending}
              onStart={onStart}
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
  busy,
  pending,
  onStart,
}: {
  childId: string;
  agentId: string | null;
  busy: boolean;
  pending: boolean;
  onStart?: (text: string) => Promise<void>;
}) {
  const send = useChatStore((s) => s.send);
  const clearSideChatDraft = useChatStore((s) => s.clearSideChatDraft);
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [autoSend, setAutoSend] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const voiceSnapshotRef = useRef("");
  const dictation = useDictationInsert(text, setText, textareaRef);

  // A `/side <question>` that opened this side chat seeds a draft to SEND (not
  // just populate). Consumed once on mount; the send waits until the child's
  // agent binding is known. Live tabs only (a pending tab has no child yet).
  useEffect(() => {
    if (pending) return;
    const draft = useChatStore.getState().sideChatDrafts[childId];
    if (draft) {
      clearSideChatDraft(childId);
      setAutoSend(draft);
    }
  }, [pending, childId, clearSideChatDraft]);
  useEffect(() => {
    if (autoSend === null || agentId === null) return;
    void send(autoSend, agentId, undefined, { pinnedConversationId: childId });
    setAutoSend(null);
  }, [autoSend, agentId, send, childId]);

  const ready = pending ? !starting : agentId !== null;
  const canSend = text.trim().length > 0 || (!pending && files.length > 0);

  const submit = () => {
    const trimmed = text.trim();
    if (pending) {
      if (trimmed.length === 0 || starting || !onStart) return;
      setStarting(true);
      // Keep the text: on success the tab closes (this unmounts); on failure
      // re-enable so the user can retry without re-typing.
      onStart(trimmed).catch(() => setStarting(false));
      return;
    }
    if ((trimmed.length === 0 && files.length === 0) || agentId === null) return;
    setText("");
    const outgoing = files;
    setFiles([]);
    void send(trimmed, agentId, outgoing.length > 0 ? outgoing : undefined, {
      pinnedConversationId: childId,
    });
  };

  return (
    <>
      {!pending && (
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/*,application/pdf,text/*,application/json"
          className="hidden"
          onChange={(event) => {
            if (event.target.files) {
              setFiles((prev) => [...prev, ...Array.from(event.target.files ?? [])]);
              event.target.value = "";
            }
          }}
        />
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
          onKeyDown: (event, intent) => {
            if (intent.shouldSubmitFromKeyboard) {
              event.preventDefault();
              submit();
            }
          },
        }}
        slots={{
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
              <Button
                type="button"
                size="icon"
                variant="default"
                aria-label="Send side question"
                disabled={!canSend || !ready || busy}
                onClick={submit}
                data-testid="side-chat-send"
              >
                <ArrowUpIcon className="size-4" />
              </Button>
            </>
          ),
        }}
      />
    </>
  );
}
