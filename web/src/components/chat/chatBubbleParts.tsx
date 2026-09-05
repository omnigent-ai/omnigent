// Bubble rendering, scroll helpers, and the working-indicator cluster for the
// chat transcript. Extracted from ChatPage.tsx so <Transcript> can import them
// without the ChatPage ↔ Transcript module cycle. ChatPage re-exports these for
// existing importers (tests + components) and imports back the few it renders.
import {
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ArrowUpIcon,
  CheckIcon,
  CopyIcon,
  FileTextIcon,
  FolderIcon,
  GitForkIcon,
  ImageIcon,
  Loader2Icon,
  XIcon,
} from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { userColor, userColorTint, userInitials } from "@/lib/userBadge";
import {
  Message,
  MessageActions,
  MessageAction,
  MessageContent,
} from "@/components/ai-elements/message";
import { Shimmer } from "@/components/ai-elements/shimmer";
import {
  BlockRenderer,
  FilePathAwareMessageResponse,
  rendersOnlyWorkedFold,
} from "@/components/blocks/BlockRenderer";
import {
  CompactionMarker,
  ErrorBanner,
  RoutingDecisionCard,
} from "@/components/blocks/StatusBlocks";
import { SystemMessageView } from "@/components/blocks/SystemMessage";
import { isSystemUserContent, parseSystemMessage } from "@/lib/systemMessage";
import { Button } from "@/components/ui/button";
import { BrandLogo } from "@/components/BrandLogo";
import { cn } from "@/lib/utils";
import { mentionItemPath, type MentionItem } from "@/lib/composerMentions";
import type { MessageContentBlock } from "@/lib/blocks";
import { ELICITATION_RESPONSE_PREFIX } from "@/lib/blocks";
import { type Bubble, type RenderItem, bubblesEqual } from "@/lib/renderItems";
import { getCurrentAuthorId } from "@/lib/identity";
import { retrySession } from "@/lib/sessionsApi";
import { useChatStore, type PendingUserMessage } from "@/store/chatStore";
import { useStickToBottomContext } from "use-stick-to-bottom";
import { UserMessageNav } from "@/components/UserMessageNav";
import { isSessionScopedDecision, showsRoutingDecisionChip } from "@/lib/routingDecision";
import { useWorkingLabelTick } from "@/hooks/useWorkingLabelTick";
import { useForkDialog } from "@/shell/ForkDialogContext";
import { SessionImage } from "@/components/SessionImage";
import { copyText } from "@/lib/clipboard";
import { showToast } from "@/components/ui/toast";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import type { SessionStatus } from "@/lib/types";

// Matches both wordings the native executors emit: "[Attached: <path>]"
// (claude/pi/cursor) and "[Attached file: <path>]" (codex). Capturing group
// is the path. Global so all markers in a message are found / stripped.
const ATTACHED_RE = /\[Attached(?: file)?:\s*([^\]]*)\]\s*/g;

// Author labels render only in a shared session; ChatPage provides the
// value and UserBubble reads it, so the gate lives in one place.
export const SessionSharedContext = createContext(false);

export function extractUserText(content: MessageContentBlock[]): string {
  return content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
}

// An absolute filesystem path in any form a native executor might materialize
// an upload to: POSIX ("/…"), Windows drive ("C:\…" or "C:/…"), or UNC
// ("\\host\share"). Workspace "@"-mention paths are always relative, so this
// reliably tells a materialized upload apart from a tagged workspace file
// regardless of the host OS the runner happens to be on.
function isAbsolutePath(p: string): boolean {
  return /^(\/|[A-Za-z]:[\\/]|\\\\)/.test(p);
}

/**
 * Pull the paths out of the "[Attached: …]" markers an "@"-mention adds to a
 * user message, so the bubble can show what was attached (the marker text
 * itself is stripped from the rendered text by {@link extractUserText}). A
 * trailing "/" marks a folder. Returns [] for ordinary messages.
 */
function extractAttachedPaths(content: MessageContentBlock[]): MentionItem[] {
  const text = content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("");
  const out: MentionItem[] = [];
  for (const m of text.matchAll(ATTACHED_RE)) {
    const raw = m[1].trim();
    if (!raw) continue;
    // Absolute path → a materialized upload, already shown via its file block.
    if (isAbsolutePath(raw)) continue;
    // Split a trailing ":start-end" line span back out so the chip can show
    // it without truncation (it's the whole point of a partial-file attach).
    const range = /^(.*):(\d+)-(\d+)$/.exec(raw);
    if (range) {
      out.push({
        path: range[1],
        isDir: false,
        lineRange: { start: Number(range[2]), end: Number(range[3]) },
      });
    } else {
      out.push({ path: raw.replace(/\/$/, ""), isDir: raw.endsWith("/") });
    }
  }
  return out;
}

/** Joins all `kind: "text"` items into a single markdown string for copying. */
export function collectBubbleMarkdown(items: RenderItem[]): string {
  return items
    .filter((item): item is Extract<RenderItem, { kind: "text" }> => item.kind === "text")
    .map((item) => item.text)
    .join("\n\n")
    .trim();
}

const TABLE_SEPARATOR_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;
const DISPLAY_MATH_RE = /(^|\n)\s*(\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\])/;

function isMarkdownTableRow(line: string): boolean {
  return line.trim().includes("|");
}

export function containsMarkdownTable(items: RenderItem[]): boolean {
  return items.some((item) => {
    if (item.kind !== "text") return false;
    const lines = item.text.split("\n");
    return lines.some(
      (line, index) =>
        TABLE_SEPARATOR_RE.test(line) &&
        index > 0 &&
        index < lines.length - 1 &&
        isMarkdownTableRow(lines[index - 1] ?? "") &&
        isMarkdownTableRow(lines[index + 1] ?? ""),
    );
  });
}

export function containsDisplayMath(items: RenderItem[]): boolean {
  return items.some((item) => item.kind === "text" && DISPLAY_MATH_RE.test(item.text));
}

/**
 * Build optimistic user bubbles from the pending-send queue.
 *
 * Author priority per bubble: `p.author` (captured at send time for
 * fresh sends, or from the snapshot's `created_by` for replayed entries)
 * falls back to `selfAuthor` (the current viewer's identity).
 *
 * @param pending - the queued optimistic sends, in FIFO order.
 * @param selfAuthor - the viewer's attribution id, or null.
 */
export function buildPendingBubbles(
  pending: PendingUserMessage[],
  selfAuthor: string | null,
): Bubble[] {
  return pending.map((p) => {
    const author = p.author ?? selfAuthor;
    return {
      kind: "user",
      // No server item id yet; tempId keeps React keys stable until promotion.
      itemId: p.tempId,
      content: p.content,
      ...(author !== null ? { createdBy: author } : {}),
      // Stamped once at send time; absent for snapshot-replayed entries,
      // which show no timestamp rather than a re-stamped render time.
      ...(p.createdAtS !== undefined ? { createdAtS: p.createdAtS } : {}),
    };
  });
}

// A committed bubble that exists ONLY to render one or more
// REQUEST-phase policy elicitation cards. See mergePendingBubbles /
// reorderCommittedRequestElicitations for how the prompt stays above the card.
function isStandaloneElicitationBubble(bubble: Bubble): boolean {
  return (
    bubble.kind === "assistant" &&
    bubble.responseId.startsWith(ELICITATION_RESPONSE_PREFIX) &&
    bubble.items.length > 0 &&
    bubble.items.every(
      (it) => it.kind === "elicitation" && (it.phase === "request" || it.phase === "pre_tool_use"),
    )
  );
}

// Pull a committed REQUEST-phase elicitation card below the user message
// it gated. Returns the input array unchanged (same reference) when no swap
// applies, so the memo stays stable.
export function reorderCommittedRequestElicitations(committed: Bubble[]): Bubble[] {
  let result: Bubble[] | null = null;
  for (let i = 0; i < committed.length - 1; i += 1) {
    if (isStandaloneElicitationBubble(committed[i]!) && committed[i + 1]!.kind === "user") {
      if (result === null) result = [...committed];
      const card = result[i]!;
      result[i] = result[i + 1]!;
      result[i + 1] = card;
    }
  }
  return result ?? committed;
}

// Insertion point above a run of create-time routing chips that STARTS the
// committed timeline.
function liftAboveCreateRoutingChips(committed: Bubble[], end: number): number {
  let start = end;
  while (start > 0) {
    const chip = committed[start - 1]!;
    if (chip.kind !== "routing_decision" || !isSessionScopedDecision(chip.routing?.scope)) break;
    start -= 1;
  }
  return start === 0 ? start : end;
}

// Place optimistic pending user bubbles into the committed timeline, keeping
// the prompt above a trailing REQUEST-phase card or create-time routing chip.
export function mergePendingBubbles(committed: Bubble[], pending: Bubble[]): Bubble[] {
  if (pending.length === 0) return committed;
  let insertAt = committed.length;
  while (insertAt > 0 && isStandaloneElicitationBubble(committed[insertAt - 1]!)) {
    insertAt -= 1;
  }
  insertAt = liftAboveCreateRoutingChips(committed, insertAt);
  if (insertAt === committed.length) return [...committed, ...pending];
  return [...committed.slice(0, insertAt), ...pending, ...committed.slice(insertAt)];
}

type ElicitationItem = Extract<RenderItem, { kind: "elicitation" }>;

// A pending elicitation is unanswered — only these float to the bottom.
function isPendingElicitation(item: RenderItem): item is ElicitationItem {
  return item.kind === "elicitation" && item.status === "pending";
}

// Pending elicitation cards float to the bottom of the chat. Collect them in
// document order — oldest first, so the newest sits last, closest to composer.
export function collectPendingElicitations(bubbles: Bubble[]): ElicitationItem[] {
  const pending: ElicitationItem[] = [];
  for (const bubble of bubbles) {
    if (bubble.kind !== "assistant") continue;
    for (const item of bubble.items) {
      if (isPendingElicitation(item)) pending.push(item);
    }
  }
  return pending;
}

// Drop the pending elicitation cards from the transcript bubbles so they
// don't render twice. Returns the input array unchanged when nothing is
// pending, so the memo stays stable.
export function stripPendingElicitations(bubbles: Bubble[]): Bubble[] {
  let result: Bubble[] | null = null;
  for (let i = 0; i < bubbles.length; i += 1) {
    const bubble = bubbles[i]!;
    if (bubble.kind !== "assistant" || !bubble.items.some(isPendingElicitation)) continue;
    if (result === null) result = [...bubbles];
    result[i] = { ...bubble, items: bubble.items.filter((it) => !isPendingElicitation(it)) };
  }
  return result ?? bubbles;
}

// Hide the sub-agent spawn chips of a session whose sub-agent routing is not
// "on". Returns the input array unchanged when nothing is hidden, so the memo
// stays stable.
export function stripGatedSubagentRoutingChips(
  bubbles: Bubble[],
  subagentRoutingOverride: "on" | "off" | null,
): Bubble[] {
  if (subagentRoutingOverride === "on") return bubbles;
  const shown = bubbles.filter(
    (b) =>
      b.kind !== "routing_decision" ||
      showsRoutingDecisionChip(b.routing?.scope, subagentRoutingOverride),
  );
  return shown.length === bubbles.length ? bubbles : shown;
}

// Whether a user bubble should carry the author's avatar badge (and the
// author-tinted background): only in a shared session, only when a human
// author is attached, and NEVER on the viewer's own messages.
export function shouldShowAuthorBadge(
  author: string | undefined,
  viewerId: string | null,
  isSessionShared: boolean,
): boolean {
  return isSessionShared && author !== undefined && author !== viewerId;
}

export function computeIsWorking(sessionStatus: SessionStatus): boolean {
  return sessionStatus === "running" || sessionStatus === "waiting";
}

/** Stable React key per bubble. */
export function bubbleKey(bubble: Bubble): string {
  // Prefer stableKey (the optimistic temp id) for promoted user bubbles
  // so the key holds steady across the optimistic→committed swap on
  // `session.input.consumed` — a changing key remounts the node (flink).
  if (bubble.kind === "user") return `user:${bubble.stableKey ?? bubble.itemId}`;
  if (bubble.kind === "compaction_loading") return `compaction_loading:${bubble.itemId}`;
  if (bubble.kind === "compaction") return `compaction:${bubble.itemId}`;
  if (bubble.kind === "routing_decision") return `routing_decision:${bubble.itemId}`;
  return `assistant:${bubble.stableId}`;
}

/**
 * Playful labels the idle-but-busy indicator rotates through (one per
 * `ROTATE_MS`). Index 0 MUST stay "Working…": it's the label a fresh tick
 * (and every unit test) lands on.
 */
export const WORKING_MESSAGES = [
  "Working…",
  "Cooking…",
  "Crunching…",
  "Tinkering…",
  "Pondering…",
  "Brewing…",
] as const;

/**
 * Busy only because background tasks outlive a FINISHED turn. While the turn
 * is still active (`agentWorking`) the shimmer wins.
 */
export function isBackgroundTasksOnly(
  bgCount: number,
  blockedOn: string | null,
  agentWorking: boolean,
): boolean {
  return !agentWorking && !blockedOn && bgCount > 0;
}

/**
 * Whether the agent's own turn is in progress — server `running`/`waiting`, or
 * a local send in flight.
 */
function useAgentTurnActive(): boolean {
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const localSending = useChatStore((s) => s.status === "streaming");
  return computeIsWorking(sessionStatus) || localSending;
}

/**
 * The label shown next to the working shimmer. When the agent is parked on a
 * dialog (`blockedOn`) it says so; otherwise it rotates through
 * `WORKING_MESSAGES` by wall-clock `tick`.
 */
export function workingIndicatorLabel(tick = 0, blockedOn: string | null = null): string {
  if (blockedOn) {
    return `Blocked on: ${blockedOn}`;
  }
  return WORKING_MESSAGES[tick % WORKING_MESSAGES.length]!;
}

export function WorkingIndicator() {
  const bgCount = useChatStore((s) => s.backgroundTaskCount);
  const blockedOn = useChatStore((s) => s.blockedOn);
  const agentWorking = useAgentTurnActive();
  const tick = useWorkingLabelTick();
  // Once the turn ends but background shells outlive it, BackgroundTaskPill owns
  // the state and the shimmer stays off (it would misread as the agent still
  // thinking). While the turn is active the shimmer shows, with the pill beside it.
  if (isBackgroundTasksOnly(bgCount, blockedOn, agentWorking)) return null;
  const label = workingIndicatorLabel(tick, blockedOn);
  return (
    <>
      {/* Sole aria-live region for the working state. A stable "Working…" (not
          the rotating label) so screen readers announce the turn once, without
          re-announcing every few seconds; the visible shimmer below stays
          aria-hidden. */}
      <span role="status" aria-live="polite" className="sr-only">
        Working…
      </span>
      <Message from="assistant" data-testid="working-indicator" aria-hidden="true">
        <MessageContent>
          <div className="flex items-center gap-1.5 py-0.5">
            <BrandLogo variant="icon" className="otto-working h-4 w-auto shrink-0" />
            <Shimmer className="text-sm font-mono" duration={1.5}>
              {label}
            </Shimmer>
          </div>
        </MessageContent>
      </Message>
    </>
  );
}

/**
 * Decide whether to render the main chat's "Working…" indicator.
 *
 * @param showsWorking - True when the session snapshot or local response
 *   state says the main session is still working.
 * @param bubbles - Rendered chat bubbles currently hydrated in the main session.
 * @returns True when the standalone working indicator should render.
 */
export function shouldShowWorkingIndicator(showsWorking: boolean, bubbles: Bubble[]): boolean {
  if (!showsWorking) return false;
  return bubbles[bubbles.length - 1]?.kind !== "compaction_loading";
}

/**
 * Whether a user-role bubble is a runtime-injected `[System: ...]`
 * notification (rendered via SystemMessageView, not as a normal user bubble).
 */
export function isSystemBubble(bubble: Bubble): boolean {
  if (bubble.kind !== "user") return false;
  return isSystemUserContent(bubble.content);
}

function CompactionLoadingIndicator({ createdAtS }: { createdAtS?: number }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    // Calculate elapsed time from the actual compaction start time (if available)
    // rather than component mount time, so the timer persists across session switches.
    const startTimeMs = createdAtS != null ? createdAtS * 1000 : Date.now();

    const updateElapsed = () => {
      setElapsed(Math.round((Date.now() - startTimeMs) / 1000));
    };

    updateElapsed();
    const id = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(id);
  }, [createdAtS]);

  return (
    <Message from="assistant" data-testid="compacting-indicator">
      <MessageContent>
        <div className="flex items-center gap-2 text-sm font-mono">
          <Shimmer as="span" duration={1.5}>
            Compacting conversation…
          </Shimmer>
          {elapsed > 0 && <span className="text-muted-foreground">({elapsed}s)</span>}
        </div>
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-muted">
          <div
            className="h-full w-1/3 rounded-full bg-muted-foreground/40"
            style={{ animation: "compaction-slide 1.5s ease-in-out infinite alternate" }}
          />
        </div>
      </MessageContent>
    </Message>
  );
}

function formatBubbleTimestamp(epochSeconds: number | undefined): string | null {
  if (epochSeconds === undefined || epochSeconds === 0) return null;
  const d = new Date(epochSeconds * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  ) {
    return time;
  }
  const date = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (d.getFullYear() !== now.getFullYear()) {
    return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}, ${time}`;
  }
  return `${date}, ${time}`;
}

// Memoized so a streaming delta (which rebuilds the whole bubble array) only
// re-renders the bubble that actually changed, not every prior message's
// markdown/syntax-highlighting subtree. See `bubblesEqual`.
export const BubbleView = memo(
  function BubbleView({
    bubble,
    isLastAssistant = false,
    showsWorking = false,
  }: {
    bubble: Bubble;
    isLastAssistant?: boolean;
    showsWorking?: boolean;
  }) {
    if (bubble.kind === "user") return <UserBubble bubble={bubble} />;
    if (bubble.kind === "compaction_loading") {
      return <CompactionLoadingIndicator createdAtS={bubble.createdAtS} />;
    }
    if (bubble.kind === "compaction") return <CompactionMarker />;
    if (bubble.kind === "routing_decision") {
      return (
        <RoutingDecisionCard
          model={bubble.model}
          applied={bubble.applied}
          rationale={bubble.rationale}
          agent={bubble.agent}
          routing={bubble.routing}
        />
      );
    }
    return (
      <AssistantBubble
        bubble={bubble}
        isLastAssistant={isLastAssistant}
        showsWorking={showsWorking}
      />
    );
  },
  (prev, next) =>
    (prev.isLastAssistant ?? false) === (next.isLastAssistant ?? false) &&
    (prev.showsWorking ?? false) === (next.showsWorking ?? false) &&
    bubblesEqual(prev.bubble, next.bubble),
);

/**
 * Copy-to-clipboard handler for a message bubble's "Copy" action.
 *
 * @param getText - Produces the text to copy at click time.
 * @returns `{ isCopied, handleCopy }` for the action button.
 */
function useCopyMessage(getText: () => string): {
  isCopied: boolean;
  handleCopy: () => void;
} {
  const [isCopied, setIsCopied] = useState(false);
  const timeoutRef = useRef<number>(0);
  const isMobile = useIsMobileViewport();

  useEffect(() => () => window.clearTimeout(timeoutRef.current), []);

  const handleCopy = useCallback(() => {
    if (isCopied) return;
    const text = getText();
    if (!text) return;
    copyText(text).then(
      () => {
        setIsCopied(true);
        window.clearTimeout(timeoutRef.current);
        timeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
        if (isMobile) {
          showToast(<span className="text-ui">Copied to clipboard</span>, { duration: 1500 });
        }
      },
      (error) => {
        console.warn("Failed to copy message", error);
      },
    );
  }, [getText, isCopied, isMobile]);

  return { isCopied, handleCopy };
}

function UserBubble({ bubble }: { bubble: Extract<Bubble, { kind: "user" }> }) {
  const sessionId = useChatStore((s) => s.conversationId);
  // Author labels only matter once the session is shared with someone else.
  const isSessionShared = useContext(SessionSharedContext);
  const text = extractUserText(bubble.content);
  const images = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_image" }> => c.type === "input_image",
  );
  const fileChips = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_file" }> => c.type === "input_file",
  );
  // "@"-mentioned workspace files/folders ride in as "[Attached: …]" text
  // markers (no input_file block), so surface them as chips.
  const mentionedChips = extractAttachedPaths(bubble.content);
  // Equality selector so Zustand only re-renders the matching bubble.
  const flashing = useChatStore((s) => s.flashItemId === bubble.itemId);
  const { isCopied, handleCopy } = useCopyMessage(() => text);
  const ts = formatBubbleTimestamp(bubble.createdAtS);
  // Runtime-injected `[System: ...]` notifications ride in on role=user. When
  // the content is a pure system marker, swap in a muted centered indicator.
  if (images.length === 0 && fileChips.length === 0 && mentionedChips.length === 0) {
    const parsed = parseSystemMessage(text);
    if (parsed) return <SystemMessageView message={parsed} />;
  }
  // Badge OTHER contributors' messages only (never your own).
  const author = bubble.createdBy;
  const showAuthorBadge = shouldShowAuthorBadge(author, getCurrentAuthorId(), isSessionShared);

  return (
    <Message
      from="user"
      data-testid="message-bubble"
      data-role="user"
      data-user-message-id={bubble.itemId}
      className="max-w-[640px]"
    >
      <div className="ml-auto flex w-fit max-w-full flex-col items-end">
        {/* w-fit + ml-auto shrink-wrap the row so the author avatar sits
            immediately left of the right-aligned bubble. */}
        <div className="flex w-fit max-w-full items-center gap-1.5">
          {showAuthorBadge && author && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Avatar
                  size="sm"
                  data-testid="message-author"
                  aria-label={author}
                  className="shrink-0"
                >
                  <AvatarFallback
                    className="font-medium text-white"
                    style={{ backgroundColor: userColor(author) }}
                  >
                    {userInitials(author)}
                  </AvatarFallback>
                </Avatar>
              </TooltipTrigger>
              <TooltipContent>{author}</TooltipContent>
            </Tooltip>
          )}
          <MessageContent
            className={cn(flashing && "animate-user-msg-flash")}
            // Another contributor's bubble takes their avatar color at low
            // alpha instead of the default bg-muted.
            style={
              showAuthorBadge && author ? { backgroundColor: userColorTint(author) } : undefined
            }
          >
            {/* Inline image previews — one non-wrapping strip. */}
            {images.length > 0 && (
              <div className="mb-1.5 flex gap-2 overflow-x-auto">
                {images.map((img) =>
                  img.file_id.startsWith("pending:") ? (
                    // Upload in-flight — show a chip placeholder
                    <span
                      key={img.file_id}
                      className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
                    >
                      <ImageIcon className="size-3 shrink-0" />
                      <span className="max-w-[180px] truncate">
                        {img.filename ?? img.file_id.replace("pending:", "")}
                      </span>
                    </span>
                  ) : (
                    // Uploaded — render the actual image
                    <SessionImage
                      key={img.file_id}
                      path={
                        sessionId
                          ? `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(img.file_id)}/content`
                          : undefined
                      }
                      alt={img.filename ?? img.file_id}
                      // Sizing lives in SessionImage, which reserves a matching
                      // box so the bubble's height is settled before bytes land.
                      className="rounded-md object-contain"
                    />
                  ),
                )}
              </div>
            )}
            {/* Non-image file chips */}
            {fileChips.length > 0 && (
              <div className="mb-1.5 flex flex-wrap gap-1.5">
                {fileChips.map((att) => (
                  <span
                    key={att.file_id}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
                  >
                    <FileTextIcon className="size-3 shrink-0" />
                    <span className="max-w-[180px] truncate">{att.filename ?? att.file_id}</span>
                  </span>
                ))}
              </div>
            )}
            {/* "@"-mentioned workspace files/folders (delivered as text markers) */}
            {mentionedChips.length > 0 && (
              <div className="mb-1.5 flex flex-wrap gap-1.5">
                {mentionedChips.map((item) => (
                  <span
                    key={mentionItemPath(item)}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
                  >
                    {item.isDir ? (
                      <FolderIcon className="size-3 shrink-0" />
                    ) : (
                      <FileTextIcon className="size-3 shrink-0" />
                    )}
                    <span className="max-w-[180px] truncate" title={mentionItemPath(item)}>
                      @{item.path}
                      {item.isDir ? "/" : ""}
                    </span>
                    {item.lineRange && (
                      <span className="shrink-0">
                        :{item.lineRange.start}-{item.lineRange.end}
                      </span>
                    )}
                  </span>
                ))}
              </div>
            )}
            {/* Render user text as markdown, matching the assistant bubble.
              `breaks` keeps single newlines as line breaks. Empty text renders
              nothing rather than an empty markdown block. */}
            {text && <FilePathAwareMessageResponse breaks>{text}</FilePathAwareMessageResponse>}
          </MessageContent>
        </div>
        {/* Skip an empty row when there is neither a timestamp nor a copy
            action. 40%-visible on touch, hover/focus-reveal on desktop. */}
        {(ts || text) && (
          <div className="flex items-center justify-end gap-3 py-1 opacity-40 transition-opacity md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
            {ts && (
              <span
                className="select-none text-[11px] leading-4 text-foreground/56"
                data-testid="message-timestamp"
              >
                {ts}
              </span>
            )}
            {text && (
              <MessageActions>
                <MessageAction
                  tooltip="Copy"
                  size="icon-xxs"
                  onClick={handleCopy}
                  componentId="chat.message.copy_user"
                >
                  {isCopied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
                </MessageAction>
              </MessageActions>
            )}
          </div>
        )}
      </div>
    </Message>
  );
}

function AssistantBubble({
  bubble,
  isLastAssistant = false,
  showsWorking = false,
}: {
  bubble: Extract<Bubble, { kind: "assistant" }>;
  isLastAssistant?: boolean;
  showsWorking?: boolean;
}) {
  // The walker only emits an assistant bubble when at least one assistant-side
  // block exists. The "Working…" shimmer for the empty-items / streaming gap
  // is rendered at the page level, not inside this component.
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const conversationId = useChatStore((s) => s.conversationId);
  // A pending elicitation means the turn is parked awaiting the user — still in
  // flight even when its lifecycle or the session status reads settled.
  const hasPendingElicitation = useChatStore((s) =>
    s.blocks.some((b) => b.type === "elicitation" && b.status === "pending"),
  );
  // Getter computes the markdown lazily at click time.
  const { isCopied, handleCopy } = useCopyMessage(() => collectBubbleMarkdown(bubble.items));
  // null outside AppShell's provider (isolated tests) → hide the action.
  const forkDialog = useForkDialog();
  const handleRetryError = useCallback(async () => {
    if (!conversationId) throw new Error("Session is not available");
    const result = await retrySession(conversationId);
    if (!result.recovered) {
      throw new Error("The session is already connected; no recovery was performed");
    }
  }, [conversationId]);

  if (bubble.items.length === 0) return null;

  const markdownText = collectBubbleMarkdown(bubble.items);
  const ts = formatBubbleTimestamp(bubble.createdAtS);

  // The bubble collapses to nothing but the "Worked for" row — its text all
  // sits inside the fold, and its answer lands in a later bubble.
  const foldOnly = rendersOnlyWorkedFold({
    items: bubble.items,
    sessionStatus,
    turnLifecycle: bubble.lifecycle,
    continued: bubble.continued,
    isLastAssistant,
    hasPendingElicitation,
    showsWorking,
  });

  // Elicitation cards want full chat-column width to match the composer.
  const hasElicitation = bubble.items.some((it) => it.kind === "elicitation");
  const isWide =
    hasElicitation || containsMarkdownTable(bubble.items) || containsDisplayMath(bubble.items);
  // An error banner's dashed rule spans the full chat column.
  const hasError = bubble.items.some((it) => it.kind === "error");
  // A bubble carrying an error but no prose stands alone as a thread-level
  // element — the hover footer belongs to assistant text, not to the error.
  const errorOnly = hasError && !markdownText;
  const spansFullColumn = isWide || hasError;

  return (
    <>
      <Message
        from="assistant"
        data-testid="message-bubble"
        data-role="assistant"
        className={
          spansFullColumn ? "max-w-full" : "max-w-3xl min-[2561px]:max-w-[clamp(56rem,30vw,64rem)]"
        }
      >
        {/* A fold-only bubble takes w-full at the ordinary max-w-3xl cap rather
            than shrink-wrapping to the summary row's ~110px. */}
        <MessageContent className={spansFullColumn || foldOnly ? "w-full" : undefined}>
          <BlockRenderer
            items={bubble.items}
            sessionStatus={sessionStatus}
            turnLifecycle={bubble.lifecycle}
            workedForS={bubble.workedForS}
            continued={bubble.continued}
            isLastAssistant={isLastAssistant}
            hasPendingElicitation={hasPendingElicitation}
            lastActivityAtS={bubble.lastActivityAtS}
            showsWorking={showsWorking}
            onRetryError={handleRetryError}
          />
        </MessageContent>
        {bubble.lifecycle === "cancelled" && (
          <p
            className="mt-1 flex items-center gap-1 text-sm text-muted-foreground"
            data-testid="assistant-interrupted-indicator"
          >
            <XIcon className="size-3" aria-hidden="true" />
            <span>Interrupted</span>
          </p>
        )}
        {/* Skipped on a fold-only bubble, when there is neither a timestamp nor
            actions, and on an error-only bubble. Order: actions, then timestamp. */}
        {!foldOnly && !errorOnly && (ts || markdownText) && (
          <div className="flex items-center gap-3 py-1 opacity-40 transition-opacity md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
            {markdownText && (
              <MessageActions>
                <MessageAction
                  tooltip="Copy"
                  size="icon-xxs"
                  onClick={handleCopy}
                  componentId="chat.message.copy_assistant"
                >
                  {isCopied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
                </MessageAction>
                {/* Fork from this response: clone the session with history
                    truncated after this turn. Hidden while streaming and when
                    the session can't be forked. */}
                {forkDialog?.canFork && bubble.lifecycle !== "streaming" && (
                  <MessageAction
                    tooltip="Fork from here"
                    size="icon-xxs"
                    data-testid="fork-from-response"
                    onClick={() => forkDialog.openForkDialog({ upToResponseId: bubble.responseId })}
                    componentId="chat.message.fork"
                  >
                    <GitForkIcon size={14} />
                  </MessageAction>
                )}
              </MessageActions>
            )}
            {ts && (
              <span
                className="select-none text-[11px] leading-4 text-foreground/56"
                data-testid="message-timestamp"
              >
                {ts}
              </span>
            )}
          </div>
        )}
      </Message>

      {/* Surface a turn-level failure as the same destructive pill an error
          block renders — never raw red text. */}
      {bubble.lifecycle === "failed" && bubble.error && (
        <ErrorBanner message={bubble.error} source="" code="" />
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Scroll helpers — rendered inside <Conversation> / as its siblings.
// ---------------------------------------------------------------------------

export function UserMessageNavConnected(props: React.ComponentProps<typeof UserMessageNav>) {
  const { isAtBottom } = useStickToBottomContext();
  return (
    <UserMessageNav
      {...props}
      // Mobile-only: the TurnRail replaces these buttons on desktop. Hidden at
      // the bottom on mobile too. Keyboard ⌘⌥↑↓ still works on all sizes.
      className={cn(props.className, "md:hidden", isAtBottom && "max-md:hidden")}
    />
  );
}

/**
 * Forces the conversation back to the bottom when this client submits a new
 * message.
 */
export function ScrollToBottomOnSend({ nonce }: { nonce: number }) {
  const { scrollToBottom } = useStickToBottomContext();

  useLayoutEffect(() => {
    if (nonce === 0) return;
    scrollToBottom("instant");
    requestAnimationFrame(() => scrollToBottom("instant"));
  }, [nonce, scrollToBottom]);

  return null;
}

/** Keep bottom-locked readers pinned when the composer changes viewport height. */
export function KeepBottomOnViewportResize() {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef?: React.RefObject<HTMLElement>;
  };
  const scrollRef = ctx.scrollRef;
  const state = ctx.state;
  const scrollToBottom = ctx.scrollToBottom;

  useEffect(() => {
    const el = scrollRef?.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const isPhysicallyAtBottom = () => el.scrollHeight - el.clientHeight - el.scrollTop <= 1;
    let wasBottomLocked = state.isAtBottom && !state.escapedFromLock && isPhysicallyAtBottom();
    let clientHeight = el.clientHeight;
    let frame: number | null = null;
    const onScroll = () => {
      wasBottomLocked = isPhysicallyAtBottom();
    };
    const observer = new ResizeObserver(() => {
      const nextHeight = el.clientHeight;
      if (nextHeight === clientHeight) return;
      clientHeight = nextHeight;
      if (!wasBottomLocked) return;
      // Gecko delivers this callback before the frame paints, but the
      // library's scrollToBottom always lands a frame later (it defers
      // through a rAF promise), so the shrink paints with the transcript
      // shoved up before the follow-up pin snaps it back — a visible
      // bounce on every wrapped composer line. Pin synchronously here
      // first; the async pins below stay as a safety net for engines that
      // deliver the callback after paint. Target the library's park
      // position (one pixel short) so the settle is identical whether our
      // write or the library's lands last.
      el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 1);
      scrollToBottom("instant");
      if (frame !== null) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        frame = null;
        el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 1);
        scrollToBottom("instant");
      });
    });
    el.addEventListener("scroll", onScroll, { passive: true });
    observer.observe(el);
    return () => {
      el.removeEventListener("scroll", onScroll);
      observer.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [scrollRef, scrollToBottom, state]);

  return null;
}

export function HistoryLoadingIndicator() {
  return (
    <div
      role="status"
      className="flex items-center justify-center gap-2 py-2 text-muted-foreground text-ui"
    >
      <Loader2Icon className="size-4 animate-spin" aria-hidden />
      Loading earlier messages…
    </div>
  );
}

/**
 * Builds the initial history window, then keeps loading near the top. The fetch
 * fires this many viewports from the top; it has to be generous because the
 * browser suppresses scroll anchoring at offset 0.
 */
const HISTORY_LOAD_TOP_VIEWPORTS = 2.5;
/** Floor for very short viewports, where 2.5x would still be a few hundred px. */
const HISTORY_LOAD_TOP_MIN_PX = 1200;

function historyLoadThreshold(el: HTMLElement): number {
  return Math.max(HISTORY_LOAD_TOP_MIN_PX, el.clientHeight * HISTORY_LOAD_TOP_VIEWPORTS);
}

/** Finger travel before a touch drag counts as "show me what's above". */
const TOUCH_DRAG_SLOP_PX = 8;

/**
 * Follow-up pages one gesture may chain beyond the page it fetched itself.
 * Settled tool-heavy turns mount folded, so a fetched page can land near-zero
 * height; a fresh gesture grants a fresh budget so older history stays reachable
 * at a reader-paced rate instead of a runaway loop.
 */
const PREPEND_CHAIN_PAGES_PER_GESTURE = 2;

/** Quiet gap after which the next wheel-up tick counts as a new gesture. */
const WHEEL_GESTURE_QUIET_MS = 300;

export function HistoryAutoLoader({
  scrollElement,
}: {
  scrollElement?: HTMLElement | null;
} = {}) {
  // useStickToBottomContext exposes scrollRef in the runtime context even though
  // the public TS types only declare isAtBottom and scrollToBottom. Cast to it.
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef: React.RefObject<HTMLElement>;
  };
  const historyGeneration = useChatStore((s) => s.historyGeneration);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);
  // A successful page updates this cursor in the same store transaction that
  // prepends its items and clears loadingMoreHistory.
  const oldestItemId = useChatStore((s) => s.oldestItemId);
  const generationRef = useRef(historyGeneration);
  const [scrollRevision, setScrollRevision] = useState(0);
  const handledScrollRevisionRef = useRef(scrollRevision);
  const oldestItemIdRef = useRef(oldestItemId);
  // Whether the reader has asked to move the transcript upward yet. Intent, not
  // movement: a window taller than the transcript has no scroll range at all.
  const scrolledUpRef = useRef(false);
  const lastScrollTopRef = useRef<number | null>(null);
  const touchStartYRef = useRef<number | null>(null);
  // Whether the current touch sequence already granted its gesture budget.
  const touchGestureSpentRef = useRef(false);
  const lastWheelUpAtRef = useRef(Number.NEGATIVE_INFINITY);
  // Prepend-fed fetches left before the chain must wait for a fresh gesture.
  const chainBudgetRef = useRef(PREPEND_CHAIN_PAGES_PER_GESTURE);

  // Position across a prepend is held by native scroll anchoring, not by this
  // component. Writing scrollTop here instead used to interrupt the reader's
  // gesture.
  useLayoutEffect(() => {
    const el = scrollElement ?? ctx.scrollRef?.current;
    if (!el) return;
    lastScrollTopRef.current = el.scrollTop;
    const noteUpwardGesture = () => {
      scrolledUpRef.current = true;
      chainBudgetRef.current = PREPEND_CHAIN_PAGES_PER_GESTURE;
      setScrollRevision((revision) => revision + 1);
    };
    const handleScroll = () => {
      const previous = lastScrollTopRef.current;
      lastScrollTopRef.current = el.scrollTop;
      // Only an upward move counts.
      if (previous !== null && el.scrollTop < previous - 0.5) {
        scrolledUpRef.current = true;
        chainBudgetRef.current = PREPEND_CHAIN_PAGES_PER_GESTURE;
      }
      setScrollRevision((revision) => revision + 1);
    };
    const handleWheel = (event: WheelEvent) => {
      if (event.deltaY >= 0) return;
      const now = performance.now();
      const newGesture = now - lastWheelUpAtRef.current > WHEEL_GESTURE_QUIET_MS;
      lastWheelUpAtRef.current = now;
      if (newGesture) noteUpwardGesture();
    };
    const handleTouchStart = (event: TouchEvent) => {
      touchStartYRef.current = event.touches[0]?.clientY ?? null;
      touchGestureSpentRef.current = false;
    };
    const handleTouchMove = (event: TouchEvent) => {
      const start = touchStartYRef.current;
      const current = event.touches[0]?.clientY;
      if (start === null || current === undefined || current <= start + TOUCH_DRAG_SLOP_PX) return;
      if (touchGestureSpentRef.current) return;
      touchGestureSpentRef.current = true;
      noteUpwardGesture();
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    el.addEventListener("wheel", handleWheel, { passive: true });
    el.addEventListener("touchstart", handleTouchStart, { passive: true });
    el.addEventListener("touchmove", handleTouchMove, { passive: true });
    return () => {
      el.removeEventListener("scroll", handleScroll);
      el.removeEventListener("wheel", handleWheel);
      el.removeEventListener("touchstart", handleTouchStart);
      el.removeEventListener("touchmove", handleTouchMove);
    };
  }, [ctx.scrollRef, scrollElement]);

  // The single paging effect. Fetches are driven by user scrolls or a changed
  // oldest item, including a visually height-neutral prepend.
  useLayoutEffect(() => {
    const el = scrollElement ?? ctx.scrollRef?.current;
    if (!el) return;

    const generationChanged = generationRef.current !== historyGeneration;
    const itemsChanged = !generationChanged && oldestItemIdRef.current !== oldestItemId;
    const scrollPositionChanged =
      !generationChanged && handledScrollRevisionRef.current !== scrollRevision;
    oldestItemIdRef.current = oldestItemId;
    handledScrollRevisionRef.current = scrollRevision;

    if (generationChanged) {
      generationRef.current = historyGeneration;
      // A new window is a new open: require a fresh upward scroll.
      scrolledUpRef.current = false;
      chainBudgetRef.current = PREPEND_CHAIN_PAGES_PER_GESTURE;
      lastScrollTopRef.current = el.scrollTop;
    }

    const state = useChatStore.getState();

    // Reader-driven only. The bind fetches its whole window in one request, and
    // this waits for the reader to actually scroll up.
    if (
      !scrolledUpRef.current ||
      !state.oldestItemId ||
      !state.hasMoreHistory ||
      state.loadingMoreHistory ||
      !(itemsChanged || scrollPositionChanged) ||
      el.scrollTop >= historyLoadThreshold(el)
    ) {
      return;
    }

    // A prepend re-feeding the chain spends gesture budget: without a bound,
    // folded (height-neutral) pages would re-feed fetches until history ran out.
    if (itemsChanged && !scrollPositionChanged) {
      if (chainBudgetRef.current <= 0) return;
      chainBudgetRef.current -= 1;
    }

    void state.loadMoreHistory();
  }, [
    ctx.scrollRef,
    historyGeneration,
    loadingMoreHistory,
    oldestItemId,
    scrollElement,
    scrollRevision,
  ]);

  // No visible control — history loads purely on scroll-up.
  return null;
}

/** Top inset for a pinned anchor: 16px beyond the fade's fully opaque edge. */
const PINNED_ANCHOR_TOP_GAP_PX = 96;

/**
 * Ceiling on the reserved space, as a share of the viewport. Capping it keeps
 * most of the viewport showing real messages.
 */
const MAX_RESERVED_VIEWPORT_FRACTION = 1 / 3;

/**
 * Trailing spacer that pins the initially loaded turn's anchor to the top of
 * the viewport. The anchor is captured once when the hydrated chat surface
 * mounts, so live sends consume the reserved space instead of jumping to top.
 */
export function LatestTurnSpacer({
  scrollElement,
  // Gap left above the pinned anchor. Defaults to clearing the top fade band;
  // with the Plan accordion pinned above (fade dropped, container already below
  // the header), the caller passes the small content inset so a framed turn
  // rests just below the accordion instead of 80px lower.
  topGapPx = PINNED_ANCHOR_TOP_GAP_PX,
  // Set to the spacer's latest measure() so a sibling (the composer's
  // same-task growth pin) can re-measure before reading scroll geometry —
  // the spacer's own ResizeObserver delivery runs a frame later, and the
  // intervening paint is the visible transcript jump.
  measureRef,
}: {
  scrollElement?: HTMLElement | null;
  topGapPx?: number;
  measureRef?: React.RefObject<(() => void) | null>;
} = {}) {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef: React.RefObject<HTMLElement>;
  };
  // Block changes remeasure the frozen anchor; streaming growth is covered by
  // the ResizeObserver. The hydration gate remounts this component on a
  // conversation switch, which captures that conversation's initial anchor.
  const blockCount = useChatStore((s) => s.blocks.length);
  const spacerRef = useRef<HTMLDivElement>(null);
  // `undefined` means capture has not run; `null` is a completed capture with
  // no suitable initial anchor (for example a brand-new empty conversation).
  const initialAnchorRef = useRef<HTMLElement | null | undefined>(undefined);
  const initialCommittedUserIdsRef = useRef<Set<string> | null>(null);
  if (initialCommittedUserIdsRef.current === null) {
    const ids = new Set<string>();
    for (const block of useChatStore.getState().blocks) {
      if (
        block.type === "user_message" &&
        !isSystemUserContent(block.content) &&
        block.ctx.itemId !== null
      ) {
        ids.add(block.ctx.itemId);
      }
    }
    initialCommittedUserIdsRef.current = ids;
  }

  const measure = useCallback(() => {
    const scrollEl = scrollElement ?? ctx.scrollRef?.current;
    const spacerEl = spacerRef.current;
    if (!scrollEl || !spacerEl) return;
    if (initialAnchorRef.current === undefined) {
      // Match DOM bubbles against committed blocks so an optimistic pending
      // send visible during this first layout can never become the anchor.
      const users = scrollEl.querySelectorAll<HTMLElement>(
        '[data-role="user"][data-user-message-id]',
      );
      let initialUser: HTMLElement | null = null;
      for (let index = users.length - 1; index >= 0; index -= 1) {
        const candidate = users[index]!;
        const itemId = candidate.dataset.userMessageId;
        if (itemId !== undefined && initialCommittedUserIdsRef.current!.has(itemId)) {
          initialUser = candidate;
          break;
        }
      }
      const texts = scrollEl.querySelectorAll<HTMLElement>(
        '[data-testid="assistant-text-section"]',
      );
      initialAnchorRef.current = initialUser ?? texts[texts.length - 1] ?? null;
    }
    const anchor = initialAnchorRef.current;
    if (!anchor) {
      // Do not let the always-mounted sentinel become a zero-height flex item.
      spacerEl.style.display = "none";
      return;
    }
    // rect diffs are scroll-invariant, and the spacer's top is fixed by the
    // content above it, so this is stable across the height we're about to set.
    const spacerRect = spacerEl.getBoundingClientRect();
    const anchorToEnd = spacerRect.top - anchor.getBoundingClientRect().top;
    // The content column's trailing padding sits below the spacer and scrolls
    // with it; leaving it out of the reservation keeps the document from
    // outgrowing the viewport by that padding.
    const trailing = spacerEl.parentElement
      ? Math.max(0, spacerEl.parentElement.getBoundingClientRect().bottom - spacerRect.bottom)
      : 0;
    const viewport = scrollEl.clientHeight;
    const next = Math.max(
      0,
      Math.min(
        viewport - anchorToEnd - topGapPx - trailing,
        viewport * MAX_RESERVED_VIEWPORT_FRACTION,
      ),
    );
    const current = Number.parseFloat(spacerEl.style.height) || 0;
    if (Math.abs(current - next) >= 1) spacerEl.style.height = `${next}px`;
  }, [ctx.scrollRef, scrollElement, topGapPx]);

  useLayoutEffect(() => {
    measure();
  }, [measure, blockCount]);

  useLayoutEffect(() => {
    if (!measureRef) return;
    measureRef.current = measure;
    return () => {
      if (measureRef.current === measure) measureRef.current = null;
    };
  }, [measure, measureRef]);

  useLayoutEffect(() => {
    const scrollEl = scrollElement ?? ctx.scrollRef?.current;
    const contentEl = spacerRef.current?.parentElement;
    if (!scrollEl || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(scrollEl); // viewport (clientHeight) changes
    if (contentEl) observer.observe(contentEl); // streaming / reflow growth
    return () => observer.disconnect();
  }, [ctx.scrollRef, measure, scrollElement]);

  return <div ref={spacerRef} aria-hidden style={{ flexShrink: 0 }} />;
}

/**
 * The conversation's scroll container plus the minimal StickToBottom controls
 * the JumpToTopButton needs to override the library's bottom-lock.
 */
export interface ConversationScroller {
  el: HTMLElement;
  state: { isAtBottom: boolean; escapedFromLock: boolean };
  stopScroll: () => void;
}

/**
 * Lifts the StickToBottom scroll container (and lock controls) out of the
 * context so a sibling rendered *outside* `<Conversation>` can still read and
 * drive it. Renders nothing.
 */
export function ConversationScrollRefBridge({
  onScroller,
}: {
  onScroller: (s: ConversationScroller | null) => void;
}) {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef: React.RefObject<HTMLElement>;
    state: ConversationScroller["state"];
    stopScroll: () => void;
  };
  useEffect(() => {
    // Runs after commit, when StickToBottom has populated scrollRef.current.
    const el = ctx.scrollRef?.current ?? null;
    onScroller(el ? { el, state: ctx.state, stopScroll: ctx.stopScroll } : null);
    return () => onScroller(null);
  }, [ctx.scrollRef, ctx.state, ctx.stopScroll, onScroller]);
  return null;
}

/**
 * Hover-revealed "Jump to top" pill. Hovering near the top edge surfaces a pill
 * at the fade border; clicking it pages in every older history block and then
 * scrolls to the very first message.
 *
 * @param containerEl - The conversation wrapper; hover/anchor reference.
 * @param scroller - Scroll container + lock controls (ConversationScrollRefBridge).
 * @param hasMoreHistory - Whether older messages exist before the loaded window.
 */
export function JumpToTopButton({
  containerEl,
  scroller,
  hasMoreHistory,
}: {
  containerEl: HTMLElement | null;
  scroller: ConversationScroller | null;
  hasMoreHistory: boolean;
}) {
  const [atTop, setAtTop] = useState(true);
  const [hovering, setHovering] = useState(false);
  const [jumping, setJumping] = useState(false);
  // Reveal the pill while the user is scrolling up, then fade it back out.
  const [scrolledUp, setScrolledUp] = useState(false);

  // How long the pill lingers after the last upward scroll before fading out.
  const SCROLL_REVEAL_MS = 2000;

  // Pixels below the conversation's top edge that count as "hovering the top".
  const HOVER_BAND_PX = 140;

  // Hover detection on the wrapper so the pill (a wrapper child) stays in-band.
  useEffect(() => {
    if (!containerEl) return;
    const onMove = (e: MouseEvent) => {
      const next = e.clientY - containerEl.getBoundingClientRect().top < HOVER_BAND_PX;
      setHovering((prev) => (prev === next ? prev : next));
    };
    const onLeave = () => setHovering(false);
    containerEl.addEventListener("mousemove", onMove, { passive: true });
    containerEl.addEventListener("mouseleave", onLeave);
    return () => {
      containerEl.removeEventListener("mousemove", onMove);
      containerEl.removeEventListener("mouseleave", onLeave);
    };
  }, [containerEl]);

  // Track whether the loaded window is scrolled to its very top, and reveal the
  // pill whenever the user scrolls up (auto-hiding after they pause).
  const scrollEl = scroller?.el ?? null;
  useEffect(() => {
    if (!scrollEl) return;
    let lastTop = scrollEl.scrollTop;
    let hideTimer: ReturnType<typeof setTimeout> | undefined;
    const onScroll = () => {
      const top = scrollEl.scrollTop;
      const next = top <= 1;
      const atBottom = top >= scrollEl.scrollHeight - scrollEl.clientHeight - 1;
      setAtTop((prev) => (prev === next ? prev : next));
      // Upward scroll (and not already pinned to the top): show the pill and
      // (re)arm the idle timer that fades it out once scrolling settles.
      if (top < lastTop - 1 && top > 1 && !atBottom) {
        setScrolledUp(true);
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => setScrolledUp(false), SCROLL_REVEAL_MS);
      } else if (top > lastTop + 1 || atBottom) {
        clearTimeout(hideTimer);
        setScrolledUp(false);
      }
      lastTop = top;
    };
    onScroll();
    scrollEl.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      clearTimeout(hideTimer);
      scrollEl.removeEventListener("scroll", onScroll);
    };
  }, [scrollEl]);

  // Somewhere to go: older pages exist, or we're scrolled down within the window.
  const canJump = hasMoreHistory || !atTop;
  const visible = jumping || ((hovering || scrolledUp) && canJump);

  const jumpToTop = useCallback(async () => {
    if (!scroller) return;
    const { el, state, stopScroll } = scroller;
    const nextFrame = () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => resolve());
      });
    setJumping(true);
    try {
      // Release StickToBottom's bottom-lock so prepend-driven scrolls bail
      // instead of yanking the view back to the bottom.
      stopScroll();
      state.isAtBottom = false;
      state.escapedFromLock = true;

      // Page in every older block before scrolling. loadMoreHistory serializes
      // via its own guard; the rAF wait yields a frame for the prepend to
      // commit. The iteration cap is a backstop against a server that never
      // reports done.
      /* oxlint-disable no-await-in-loop */
      for (let i = 0; i < 1000 && useChatStore.getState().hasMoreHistory; i++) {
        await useChatStore.getState().loadMoreHistory();
        // Keep the lock released — a prepend that briefly lands us near the
        // bottom can otherwise re-arm it via the library's scroll handler.
        state.isAtBottom = false;
        state.escapedFromLock = true;
        await nextFrame();
      }
      // Pin to the very top, re-asserting across frames until it holds.
      for (let i = 0, stable = 0; i < 60 && stable < 2; i++) {
        if (el.scrollTop === 0) stable += 1;
        else {
          el.scrollTop = 0;
          stable = 0;
        }
        await nextFrame();
      }
      /* oxlint-enable no-await-in-loop */
    } finally {
      setJumping(false);
    }
  }, [scroller]);

  return (
    <div
      // top 50px centers the pill on the chat-scroll-fade border, just below the
      // h-14 ChatHeader. z-40 > header z-30. On iOS the header/fade shift down by
      // the safe-area inset, so add --omnigent-inset-top (0px off-shell).
      style={{ top: "calc(50px + var(--omnigent-inset-top))" }}
      className={cn(
        "pointer-events-none absolute inset-x-0 z-40 flex justify-center transition-opacity duration-150",
        visible ? "opacity-100" : "opacity-0",
      )}
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={jumping}
        onClick={() => void jumpToTop()}
        aria-label="Jump to the first message"
        componentId="chat.nav.jump_to_top"
        // When hidden keep the button out of the tab order and a11y tree.
        tabIndex={visible ? 0 : -1}
        aria-hidden={!visible}
        className={cn(
          "h-7 gap-1.5 rounded-full px-3 text-sm shadow-sm",
          // Force an OPAQUE background in both themes and on hover, so the faded
          // chat text behind the pill doesn't bleed through.
          "bg-background hover:bg-background hover:brightness-95",
          "dark:bg-background dark:hover:bg-background dark:hover:brightness-125",
          visible ? "pointer-events-auto" : "pointer-events-none",
        )}
      >
        {jumping ? (
          <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <ArrowUpIcon className="size-3.5" aria-hidden />
        )}
        {jumping ? "Loading history…" : "Jump to top"}
      </Button>
    </div>
  );
}
