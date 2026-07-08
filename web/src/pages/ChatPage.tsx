import {
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  createContext,
  memo,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArrowUpIcon,
  BotIcon,
  CheckIcon,
  AlertTriangleIcon,
  ChevronDownIcon,
  CornerUpLeftIcon,
  CopyIcon,
  FileTextIcon,
  FolderIcon,
  GitBranchIcon,
  GitForkIcon,
  ImageIcon,
  Loader2Icon,
  MessageSquareIcon,
  PaperclipIcon,
  SquareIcon,
  TerminalIcon,
  WifiOffIcon,
  XIcon,
} from "lucide-react";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { userColor, userColorTint, userInitials } from "@/lib/userBadge";
import { useNavigate, useParams } from "@/lib/routing";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { useStickToBottomContext } from "use-stick-to-bottom";
import {
  Message,
  MessageActions,
  MessageAction,
  MessageContent,
} from "@/components/ai-elements/message";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { ElicitationCard } from "@/components/blocks/ApprovalCard";
import { BlockRenderer, FilePathAwareMessageResponse } from "@/components/blocks/BlockRenderer";
import { CompactionMarker, RoutingDecisionChip } from "@/components/blocks/StatusBlocks";
import { SystemMessageView } from "@/components/blocks/SystemMessage";
import { parseSystemMessage } from "@/lib/systemMessage";
import { Button } from "@/components/ui/button";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { cn } from "@/lib/utils";
import { QueuedMessagesStrip } from "@/pages/QueuedMessagesStrip";
import { validateAttachments } from "@/lib/attachments";
import { useSurfaceFrontmost } from "@/hooks/useNativeServerSwitcher";
import {
  isIOSShell,
  onNativeSidebarDrag,
  onNativeViewModeChanged,
  setNativeServerSwitcherHidden,
  setNativeViewMode,
} from "@/lib/nativeBridge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { type Agent, useSessionAgent, useAgents } from "@/hooks/useAgents";
import { agentDisplayLabel } from "@/components/AgentInfo";
import { BRAIN_HARNESS_LABELS, useBrainHarnessLabels } from "@/lib/agentLabels";
import { useConversations } from "@/hooks/useConversations";
import { usePermissions } from "@/hooks/usePermissions";
import type { CodexModelOption, SandboxStatus, Session, SessionStatus } from "@/lib/types";
import { usePromptHistory } from "@/hooks/usePromptHistory";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useIOSNativeKeyboardVisible } from "@/hooks/useIOSNativeKeyboardInset";
import type { MessageContentBlock } from "@/lib/blocks";
import {
  derivePermissionLevel,
  isOwnerLevel,
  isSessionSharedWithOthers,
} from "@/lib/permissionsApi";
import {
  type Bubble,
  type RenderItem,
  type BubbleCache,
  buildBubbles,
  bubblesEqual,
  createBubbleCache,
} from "@/lib/renderItems";
import { getCurrentAuthorId } from "@/lib/identity";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { codexEffortLevelsForModel, findCodexModelOption } from "@/lib/codexNativeModels";
import {
  composerAttachmentKey,
  consumePendingInitialPrompt,
  type PendingInitialPrompt,
  type PendingUserMessage,
  type QueuedMessage,
  useChatStore,
} from "@/store/chatStore";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import {
  buildMentionPreamble,
  detectMentionAt,
  type MentionItem,
  mentionItemPath,
  mentionMarkerFor,
  type MentionState,
  parseMentionToken,
  rankMentionEntries,
} from "@/lib/composerMentions";
import { useMentionBrowser } from "@/hooks/useMentionBrowser";
// Re-exported so existing tests importing these from "./ChatPage" keep working
// after the pure helpers moved to the shared lib.
export { detectMentionAt, mentionMarkerFor };
export type { MentionItem, MentionState };
import { useSession } from "@/hooks/useSession";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useRefreshSessionStateOnRunnerOnline } from "@/hooks/useSessionOnlineRefresh";
import {
  type LivenessRow,
  type SessionLiveness,
  livenessRowFromSession,
  useSessionLiveness,
} from "@/hooks/useSessionLiveness";
import { useMarkConversationSeen } from "@/hooks/useUnseenConversations";
import { useUserMessageNav } from "@/hooks/useUserMessageNav";
import { useWorkingLabelTick } from "@/hooks/useWorkingLabelTick";
import { UserMessageNav } from "@/components/UserMessageNav";
import { HostBadge } from "@/components/HostBadge";
import {
  BUILTIN_SLASH_COMMANDS,
  isSlashCommandText,
  SlashCommandMenu,
} from "@/components/SlashCommandMenu";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import {
  useWorkspaceAllFiles,
  useWorkspaceDirectory,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import {
  IntelligentModelControl,
  type CostRoutingVerdict,
  isCostRoutingSession,
  parseCostRoutingVerdict,
} from "@/components/CostRoutingControl";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { MainTerminalView } from "@/shell/MainTerminalView";
import { UNTITLED_CONVERSATION_LABEL } from "@/shell/sidebarNav";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { ResumeWithDirectoryDialog } from "@/shell/ResumeWithDirectoryDialog";
import { ReconnectSessionDialog } from "@/shell/ReconnectSessionDialog";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { useForkDialog } from "@/shell/ForkDialogContext";
import { supportsEffortControl } from "@/lib/sessionCapabilities";
import { isCodexNativeSession } from "@/lib/codexPlanMode";
import { getCliServerUrl } from "@/lib/host";
import { SessionImage } from "@/components/SessionImage";
import {
  CodexGoalControl,
  CodexGoalStatusPill,
  useCodexGoalState,
  type CodexGoal,
} from "@/components/codex";

// Matches both wordings the native executors emit: "[Attached: <path>]"
// (claude/pi/cursor) and "[Attached file: <path>]" (codex). Capturing group
// is the path. Global so all markers in a message are found / stripped.
const ATTACHED_RE = /\[Attached(?: file)?:\s*([^\]]*)\]\s*/g;

function extractUserText(content: MessageContentBlock[]): string {
  return content
    .filter(
      (c): c is Extract<MessageContentBlock, { type: "input_text" }> => c.type === "input_text",
    )
    .map((c) => c.text)
    .join("")
    .replace(ATTACHED_RE, "")
    .trim();
}

/**
 * Pull the paths out of the "[Attached: …]" markers an "@"-mention adds to a
 * user message, so the bubble can show what was attached (the marker text
 * itself is stripped from the rendered text by {@link extractUserText}). A
 * trailing "/" marks a folder. Returns [] for ordinary messages.
 *
 * Explicitly *uploaded* files share this marker wording: the native executor
 * materializes the upload to disk and injects `[Attached: <abs-path>]` so the
 * vendor CLI can read it. Those uploads already ride in as an
 * `input_image`/`input_file` block (rendered as the image / a file chip), so
 * surfacing them again here would double-render — as the path of an internal
 * bridge temp dir, no less. "@"-mention paths are always workspace-relative
 * while upload markers are absolute, so skip absolute paths (see
 * {@link isAbsolutePath}).
 */
// An absolute filesystem path in any form a native executor might materialize
// an upload to: POSIX ("/…"), Windows drive ("C:\…" or "C:/…"), or UNC
// ("\\host\share"). Workspace "@"-mention paths are always relative, so this
// reliably tells a materialized upload apart from a tagged workspace file
// regardless of the host OS the runner happens to be on.
function isAbsolutePath(p: string): boolean {
  return /^(\/|[A-Za-z]:[\\/]|\\\\)/.test(p);
}

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

// Leading whitespace + the command token, so the composer overlay can tint
// just the `/skill` and leave any args in the default color.
const SLASH_COMMAND_SPLIT_RE = /^(\s*)(\/[A-Za-z0-9][\w:-]*)/;

/**
 * Split a slash-command draft into the command token and the rest, for the
 * composer highlight overlay. Returns null when the text isn't a command
 * (callers gate on `isSlashCommandText`, so a returned token is the full
 * command — never a `/etc/hosts`-style path prefix).
 */
export function splitSlashCommand(
  value: string,
): { before: string; token: string; after: string } | null {
  const m = SLASH_COMMAND_SPLIT_RE.exec(value);
  if (!m) return null;
  const [, before, token] = m;
  return { before, token, after: value.slice(before.length + token.length) };
}

/** Joins all `kind: "text"` items into a single markdown string for copying. */
export function collectBubbleMarkdown(items: RenderItem[]): string {
  return items
    .filter((item): item is Extract<RenderItem, { kind: "text" }> => item.kind === "text")
    .map((item) => item.text)
    .join("\n\n")
    .trim();
}

// All chat-column elements must share this width to stay aligned.
const CHAT_COLUMN_WIDTH = "max-w-3xl min-[1921px]:max-w-4xl min-[2561px]:max-w-5xl";

const TABLE_SEPARATOR_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/;

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

/**
 * Build optimistic user bubbles from the pending-send queue.
 *
 * Author priority per bubble: `p.author` (captured at send time for
 * fresh sends, or from the snapshot's `created_by` for replayed entries)
 * falls back to `selfAuthor` (the current viewer's identity).
 * This covers two cases:
 *   1. A fresh send: `selfAuthor` is the viewer; `p.author` is the same
 *      value stamped at `send()` time, so either works.
 *   2. A snapshot-replayed pending entry (reconnect): `p.author` carries
 *      the original sender's email from the server, so a collaborator
 *      reconnecting sees the correct attribution rather than their own
 *      email stamped on someone else's message.
 *
 * `selfAuthor` is null before identity resolves and in single-user
 * mode (no label shown).
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
    };
  });
}

// A committed bubble that exists ONLY to render one or more
// REQUEST-phase policy elicitation cards. A REQUEST-phase ASK parks the
// user message server-side (it is not persisted / consumed until the
// human approves — POLICIES.md §7.2), so the message lingers as an
// optimistic pending bubble (and later a consumed committed bubble) while
// its elicitation card arrives as a standalone committed assistant
// bubble. Used by `mergePendingBubbles` and
// `reorderCommittedRequestElicitations` to keep the prompt above the card
// that asks about it, both before and after approval.
function isStandaloneElicitationBubble(bubble: Bubble): boolean {
  // A committed assistant bubble that is ENTIRELY an elicitation card with no
  // turn to anchor to, so it must sit BELOW the user message it gated:
  //   • REQUEST-phase policy ASKs (gate the prompt before any turn), and
  //   • terminal-driven harness gates such as cursor-native `pre_tool_use`,
  //     which never emit `response_created` (blockStream stamps these with
  //     their own `elicit_*` id, so they land as standalone bubbles).
  // A `tool_call` card inside an active SDK turn renders inline — it is grouped
  // WITH the turn, so it is never an all-elicitation standalone bubble — and is
  // intentionally excluded here.
  return (
    bubble.kind === "assistant" &&
    bubble.items.length > 0 &&
    bubble.items.every(
      (it) => it.kind === "elicitation" && (it.phase === "request" || it.phase === "pre_tool_use"),
    )
  );
}

// Pull a committed REQUEST-phase elicitation card below the user message
// it gated.
//
// Once a REQUEST-phase ASK is approved, the parked user message is
// consumed and appended to `blocks` — but AFTER the elicitation card,
// which arrived (and committed) while the message was still parked
// server-side. The committed order is therefore [card, message], so the
// approved card would sit ABOVE the prompt that triggered it. Swap each
// such card with the user bubble that immediately follows it so the
// prompt stays on top, matching the pre-approval pending layout
// (`mergePendingBubbles`). A card with no following user bubble (declined
// / still pending) is left untouched. Returns the input array unchanged
// (same reference) when no swap applies, so the memo stays stable.
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

// Place optimistic pending user bubbles into the committed timeline.
//
// Pending sends normally trail everything (the input should be visible
// immediately, and they migrate into `blocks` once their
// `session.input.consumed` event lands). The exception is a REQUEST-phase
// policy ASK: that message never gets a consumed event until approval, so
// it stays pending while its elicitation card renders as a committed
// bubble — appending the pending bubble after the card would show the
// approval prompt ABOVE the message that triggered it. When the timeline
// ends in a run of such request-elicitation bubbles, splice the pending
// bubbles in just before that run so the prompt stays on top.
export function mergePendingBubbles(committed: Bubble[], pending: Bubble[]): Bubble[] {
  if (pending.length === 0) return committed;
  let insertAt = committed.length;
  while (insertAt > 0 && isStandaloneElicitationBubble(committed[insertAt - 1]!)) {
    insertAt -= 1;
  }
  if (insertAt === committed.length) return [...committed, ...pending];
  return [...committed.slice(0, insertAt), ...pending, ...committed.slice(insertAt)];
}

type ElicitationItem = Extract<RenderItem, { kind: "elicitation" }>;

// A pending elicitation is unanswered — only these float to the bottom.
function isPendingElicitation(item: RenderItem): item is ElicitationItem {
  return item.kind === "elicitation" && item.status === "pending";
}

// Pending elicitation cards float to the bottom of the chat: lifted out of
// their inline position and re-rendered as the last items in the scroll flow,
// so stick-to-bottom keeps an outstanding question in view no matter how much
// text the agent streams after it (otherwise the card scrolls up off the top
// of the viewport). Collect them in document order — oldest first, so the
// newest sits last, closest to the composer. Once answered, a card drops out
// of this list (status flips to "responded") and stays inline at its natural
// spot (it is no longer removed by `stripPendingElicitations`).
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
// don't render twice — once at the bottom, once inline. Only clones the
// assistant bubbles that actually carry a pending card; every other bubble
// keeps its reference so `BubbleView`'s memo holds. An assistant bubble left
// with no items renders nothing (`AssistantBubble` returns null), so a
// standalone elicitation bubble collapses cleanly while its gating user
// message stays put. Returns the input array unchanged when nothing is
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

// Whether a user bubble should carry the author's avatar badge (and the
// author-tinted background): only in a shared session, only when a human
// author is attached (agent/tool/system output and pre-attribution
// history leave createdBy undefined), and NEVER on the viewer's own
// messages — you know what you sent; the badge marks OTHER contributors.
export function shouldShowAuthorBadge(
  author: string | undefined,
  viewerId: string | null,
  isSessionShared: boolean,
): boolean {
  return isSessionShared && author !== undefined && author !== viewerId;
}

/**
 * Whether a submitted message should be queued rather than POSTed now.
 *
 * Queue when busy, or when this conversation already has a queued message even
 * if it reads idle: the direct-send and queue-drain paths aren't ordered, so a
 * later direct send could overtake a still-queued earlier one when status
 * flickers idle mid-queue (cursor-native). A new chat always sends.
 */
export function shouldQueueSend(
  conversationId: string | null,
  status: "idle" | "streaming",
  sessionStatus: SessionStatus,
  queuedMessages: QueuedMessage[],
): boolean {
  if (conversationId === null) return false;
  const isBusy =
    status === "streaming" || sessionStatus === "running" || sessionStatus === "waiting";
  const hasQueued = queuedMessages.some((m) => m.conversationId === conversationId);
  return isBusy || hasQueued;
}

// Author labels render only in a shared session; ChatPage provides the
// value and UserBubble reads it, so the gate lives in one place.
const SessionSharedContext = createContext(false);

// Iterate code points (not UTF-16 units) so emoji aren't cut mid-surrogate;
// prefer the last word boundary within 10 chars of the limit so we don't
// chop a word in half; trimEnd before the ellipsis so we never emit "foo  …".
function truncateTitle(raw: string, max = 60): string {
  const points = Array.from(raw);
  if (points.length <= max) return raw;
  const slice = points.slice(0, max - 1);
  const lastSpace = slice.lastIndexOf(" ");
  const cut = lastSpace > max - 10 ? lastSpace : slice.length;
  return slice.slice(0, cut).join("").trimEnd() + "…";
}

// Per-session draft storage — module-level so it survives the Composer
// unmount/remount that happens during the loading gate between session
// switches (ChatPage returns <HydratingPlaceholder /> while
// loadingConversation is true, which unmounts the entire chat surface).
// Text drafts are also persisted to sessionStorage so they survive page
// refreshes; File objects can't be serialized, so only text round-trips.
const SESSION_DRAFTS_KEY = "omnigent.sessionDrafts";

function loadDraftsFromStorage(): Map<string, { text: string; files: File[] }> {
  try {
    const raw = window.sessionStorage.getItem(SESSION_DRAFTS_KEY);
    if (!raw) return new Map();
    const entries = JSON.parse(raw) as Record<string, string>;
    const map = new Map<string, { text: string; files: File[] }>();
    for (const [id, text] of Object.entries(entries)) {
      if (text) map.set(id, { text, files: [] });
    }
    return map;
  } catch {
    return new Map();
  }
}

function saveDraftsToStorage(drafts: Map<string, { text: string; files: File[] }>): void {
  try {
    const obj: Record<string, string> = {};
    for (const [id, draft] of drafts) {
      if (draft.text) obj[id] = draft.text;
    }
    if (Object.keys(obj).length === 0) {
      window.sessionStorage.removeItem(SESSION_DRAFTS_KEY);
    } else {
      window.sessionStorage.setItem(SESSION_DRAFTS_KEY, JSON.stringify(obj));
    }
  } catch {
    // Storage full or unavailable — drafts still work in-memory.
  }
}

const sessionDrafts = loadDraftsFromStorage();

/**
 * Single component that drives the chat surface. Streaming + history
 * state lives in `useChatStore` (a Zustand store at module scope), so
 * this component is reactive but not stateful — it observes the store
 * and triggers `switchTo` when the URL changes. The store owns the
 * items fetch (no useConversationItems here).
 */
export function ChatPage() {
  const { conversationId: urlConvId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  // Optional first message handed off by the landing composer through the
  // shared chatStore (keyed by conversation id), not router state — router state
  // doesn't survive the embed's host-provided routing. Consumed read-once
  // in the effect below so a refresh/back can't replay it, then held here
  // so the auto-send effect re-runs once it's resolved.
  // Bundled with the conversation id it was consumed for so the auto-send
  // gate can reject a prompt that no longer matches the active session (the
  // session-switch leak — see shouldSendInitialPrompt). `null` until the
  // consume effect runs (or when no prompt was carried).
  const [initialPrompt, setInitialPrompt] = useState<{
    conversationId: string;
    prompt: PendingInitialPrompt;
  } | null>(null);
  // The conversation id we already auto-sent an initial prompt for, or
  // null. NOT a bare boolean: ChatPage stays mounted across `/c/:a` →
  // `/c/:b` (no route `key`), so a boolean once-guard would latch true
  // after the first auto-send and silently drop the prompt for every
  // subsequent new chat created without a full page reload. Keying the
  // guard by conversation id resets it per session while still covering
  // StrictMode's double-invoke and re-renders within one session.
  const initialPromptSentForConvRef = useRef<string | null>(null);
  // Caches the consumed prompt keyed by conversation id so the consume
  // effect is idempotent under StrictMode's setup→cleanup→setup
  // double-invoke. `consumePendingInitialPrompt` is destructive (get +
  // delete): the first invocation drains the store map, so a naive second
  // invocation would read null and last-write-wins would settle
  // `initialPrompt` to null — silently dropping the prompt in dev. By
  // memoizing the first result per conv id, the second invocation reuses
  // it. Keyed by id (not a bare value) so it still re-consumes for the
  // next conversation when ChatPage stays mounted across `/c/:a` → `/c/:b`.
  const consumedInitialPromptRef = useRef<{
    conversationId: string;
    prompt: PendingInitialPrompt | null;
  } | null>(null);
  const {
    data: agents,
    isLoading: agentsLoading,
    error: agentsError,
    refetch: refetchAgents,
  } = useAgents();
  const { data: conversationsData } = useConversations();
  const conversations = useMemo(
    () => conversationsData?.pages.flatMap((p) => p.data),
    [conversationsData],
  );

  // Clear the "unseen messages" sidebar dot for the conversation the
  // user is currently viewing. Re-fires when conversations refresh
  // (every 4 s) so messages arriving while viewing are marked seen.
  useMarkConversationSeen(urlConvId, conversations?.find((c) => c.id === urlConvId)?.updated_at);

  // Sync the store's active conversation to the URL. Single source of
  // truth: URL is what's "current"; store mirrors it. The effect is
  // the minimal unified surface for all URL change paths — sidebar
  // clicks (which also navigate), browser Back/Forward (no handler),
  // initial mount with a deep-linked URL, and the eager URL update
  // from `send` (no-op due to switchTo's self-skip).
  //
  // switchTo is async (it fetches items on conv-id transitions); we
  // intentionally don't await it here. The store's `loadingConversation` flag
  // drives the loading UI below; `conversationLoadError` drives the error UI.
  useEffect(() => {
    void useChatStore.getState().switchTo(urlConvId ?? null);
  }, [urlConvId]);

  // Server-driven redirect: when the active conversation is superseded
  // (a `session.superseded` event — e.g. a Claude `/clear` rotated it
  // away), the store records the follow-to target in
  // `redirectToConversationId`. Perform the router navigation here (the
  // store can't), replacing history so Back doesn't return to the
  // cleared session, then clear the flag so it fires exactly once. Skip
  // when we're already on the target URL.
  const redirectToConversationId = useChatStore((s) => s.redirectToConversationId);
  useEffect(() => {
    if (!redirectToConversationId) return;
    if (redirectToConversationId !== urlConvId) {
      navigate(`/c/${redirectToConversationId}`, { replace: true });
    }
    useChatStore.setState({ redirectToConversationId: null });
  }, [redirectToConversationId, urlConvId, navigate]);

  // Pull the first message the landing composer stashed for this conversation,
  // if any. Read-once (consume deletes), so a refresh/back can't replay
  // it. Runs in an effect (not render) because consume mutates the store
  // map — calling it during render would double-consume under StrictMode.
  // The per-conv-id cache (consumedInitialPromptRef) makes the consume
  // idempotent across StrictMode's double-invoke: the first run drains the
  // map and caches the result; the second run reuses the cache instead of
  // re-consuming (which would read null and drop the prompt). Resetting to
  // null when no prompt is pending clears a prior conversation's value
  // when ChatPage stays mounted across `/c/:a` → `/c/:b`.
  useEffect(() => {
    if (!urlConvId) {
      setInitialPrompt(null);
      return;
    }
    const cached = consumedInitialPromptRef.current;
    const prompt =
      cached?.conversationId === urlConvId ? cached.prompt : consumePendingInitialPrompt(urlConvId);
    consumedInitialPromptRef.current = { conversationId: urlConvId, prompt };
    setInitialPrompt(prompt === null ? null : { conversationId: urlConvId, prompt });
  }, [urlConvId]);

  // Subscribe to the bits of store state we render. Each is a
  // primitive selector so re-renders fire only when that specific
  // field changes — no `useShallow` needed.
  const blocks = useChatStore((s) => s.blocks);
  const pendingUserMessages = useChatStore((s) => s.pendingUserMessages);
  const activeResponse = useChatStore((s) => s.activeResponse);
  const interruptedResponseIds = useChatStore((s) => s.interruptedResponseIds);
  const status = useChatStore((s) => s.status);
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  // True while the session's managed-sandbox launch is still running
  // (a failed launch is NOT "launching" — it gets normal unreachable
  // handling). Overrides the liveness-derived unreachable affordances
  // below, which misread the not-yet-host-bound session as stranded.
  const sandboxLaunching = sandboxStatus !== null && sandboxStatus.stage !== "failed";
  // Read runner liveness from the app-level batch poller (see
  // RunnerHealthProvider). `undefined` = not yet polled — the indicator
  // stays hidden until the first poll for this session resolves.
  const runnerOnline = useSessionRunnerOnline(urlConvId);
  useRefreshSessionStateOnRunnerOnline(urlConvId, runnerOnline);
  // OR'd into "Working…" so cross-client turns surface a shimmer.
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const backgroundTaskCount = useChatStore((s) => s.backgroundTaskCount);
  const loadingConversation = useChatStore((s) => s.loadingConversation);
  const conversationLoadError = useChatStore((s) => s.conversationLoadError);
  const boundAgentId = useChatStore((s) => s.boundAgentId);
  const boundAgentName = useChatStore((s) => s.boundAgentName);
  // Fallback for session-scoped agents (created by `omnigent run --server`):
  // the sessions-derived list only carries id+name, so fetch the full
  // agent object for the active session. Drives the picker's
  // name/description; the same react-query cache also feeds the header
  // info icon (AgentInfoButton) its tools & policies.
  const { data: boundAgentBySession } = useSessionAgent(urlConvId ?? null);
  const hasMoreHistory = useChatStore((s) => s.hasMoreHistory);
  const loadingMoreHistory = useChatStore((s) => s.loadingMoreHistory);

  // Build bubbles once per blocks/activeResponse change. Memo here so
  // unrelated store updates (status, loading flags) don't re-walk.
  // Pending user messages (POSTed but not yet acked by
  // `session.input.consumed`) render as trailing user bubbles so the
  // input is visible immediately. They migrate into `blocks` the moment
  // their consumed event arrives.
  // Per-surface reuse cache so a streaming append rebuilds only the
  // active bubble, reusing the finalized prefix by reference.
  const bubbleCacheRef = useRef<BubbleCache>(createBubbleCache());
  const bubbles = useMemo<Bubble[]>(() => {
    // A REQUEST-phase elicitation card commits before the user message it
    // gates: while pending, the message is an optimistic trailing bubble
    // (`mergePendingBubbles` lifts it above the card); once approved, the
    // consumed message lands in `blocks` AFTER the card
    // (`reorderCommittedRequestElicitations` swaps the card below it).
    // Both keep the prompt on top across the pending → approved flip.
    const committed = reorderCommittedRequestElicitations(
      buildBubbles(blocks, activeResponse, bubbleCacheRef.current, interruptedResponseIds),
    );
    // claude-native live previews are NOT trailing bubbles — they live in
    // `blocks` as provisional `live:*` text blocks at their streamed
    // position (see chatStore), so they render in-order with later tool /
    // elicitation cards. The optimistic pending user message trails too,
    // except when the timeline ends in a REQUEST-phase elicitation card.
    if (pendingUserMessages.length === 0) return committed;
    return mergePendingBubbles(
      committed,
      buildPendingBubbles(pendingUserMessages, getCurrentAuthorId()),
    );
  }, [blocks, activeResponse, interruptedResponseIds, pendingUserMessages]);

  // Picker selection. ChatPage stays mounted across `/` to `/c/:id`,
  // so the pick survives sidebar clicks; resets on full page reload.
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const agentId = selectedAgentId ?? agents?.[0]?.id ?? null;

  // Sync the picker to the conversation's bound agent when switching.
  // `boundAgentId` is `null` on `/`, during the snapshot fetch, and
  // for legacy conversations without an agent binding — leave the
  // picker alone in those cases.
  //
  // If the bound agent isn't in the cached list (e.g. a new agent was
  // registered by a fresh `omnigent run` after the page loaded),
  // refetch so the list stays current. staleTime: Infinity means the
  // query won't self-update, so we do it manually on demand.
  useEffect(() => {
    if (boundAgentId === null) return;
    setSelectedAgentId(boundAgentId);
    if (agents && !agents.some((a) => a.id === boundAgentId)) {
      void refetchAgents();
    }
  }, [boundAgentId, agents, refetchAgents]);

  // Auto-send the first message the landing composer stashed in the chatStore,
  // exactly once per conversation. Wait until the session is hydrated
  // (stream bound) — sending before bindStream connects would lose the
  // turn's events on the no-replay live-tail stream ("no response"). We do
  // NOT wait for the runner to be online: chatStore.send pushes the
  // optimistic bubble synchronously, so it renders the instant the stream
  // binds, and the server's POST /events handler holds the request open
  // while a host-bound runner spins up (connect grace + relaunch), 503ing
  // only if no runner ever appears. So a slow runner shows the bubble
  // immediately and resolves when it connects; a genuinely dead host
  // surfaces a failed send instead of a silently-dropped prompt on an
  // empty composer. Posting through chatStore.send is
  // agent-agnostic: event agents run a turn; native terminal agents
  // (Claude Code / Codex) have the runner inject the text into their
  // CLI. The consume effect above already read-once-deleted the prompt
  // from the store, so a refresh/back has nothing to replay. The ref
  // guard (set synchronously before send, keyed by conversation id) also
  // covers StrictMode's setup→cleanup→setup double-invoke: it persists
  // across the remount, so the second setup short-circuits and the prompt
  // sends once — while still resetting for the next conversation, since
  // ChatPage stays mounted across `/c/:a` → `/c/:b`.
  useEffect(() => {
    if (
      !shouldSendInitialPrompt({
        initialPrompt: initialPrompt?.prompt.text ?? null,
        promptConversationId: initialPrompt?.conversationId ?? null,
        sentForConversationId: initialPromptSentForConvRef.current,
        conversationId: urlConvId,
        loadingConversation,
        agentId,
      })
    ) {
      return;
    }
    // TypeScript can't see through the predicate's boolean return, so
    // re-check to narrow the types for send()/the template literal. The
    // predicate already guarantees these, so this never fires at runtime.
    if (initialPrompt === null || !agentId || !urlConvId) return;
    initialPromptSentForConvRef.current = urlConvId;
    const { send, sendSlashCommand } = useChatStore.getState();
    dispatchInitialPrompt(initialPrompt.prompt, agentId, send, sendSlashCommand);
  }, [initialPrompt, urlConvId, loadingConversation, agentId]);

  // Open state owned here (not inside MainAgentSurface) so the dialog
  // survives a re-mount of the chat surface. Declared BEFORE the
  // loading/error early-returns below — hooks must run in the same
  // order every render.
  // Unbound coding-clone resume: the directory picker, plus the message
  // the user tried to send (replayed once the bind brings the runner
  // online). Declared before the early-return guards (Rules of Hooks).
  const [resumeDirDialogOpen, setResumeDirDialogOpen] = useState(false);
  // The message the user tried to send into an unbound coding clone,
  // PINNED to the session it was typed in. ChatPage stays mounted across
  // `/c/:a → /c/:b`, so without the `sessionId` pin a stashed prompt
  // would replay into whatever session is active when a runner next comes
  // online — leaking the message into a different conversation. Keyed by
  // session id, it only ever replays into the clone it was meant for.
  const [pendingResumePrompt, setPendingResumePrompt] = useState<{
    sessionId: string;
    text: string;
    files: File[];
  } | null>(null);

  // Replay the queued message once the picker's bind brings the runner
  // online — but ONLY while still viewing the session it was pinned to.
  // Waiting on runnerOnline (not firing immediately after the POST) avoids
  // racing the runner's async boot — same readiness gate the initial-prompt
  // effect uses. If the user switched away before the clone started, the
  // prompt stays pinned and waits; it never floats into another session.
  useEffect(() => {
    if (pendingResumePrompt === null || !agentId || !urlConvId) return;
    if (pendingResumePrompt.sessionId !== urlConvId) return;
    if (runnerOnline !== true) return;
    const { text, files } = pendingResumePrompt;
    setPendingResumePrompt(null);
    void useChatStore.getState().send(text, agentId, files);
  }, [pendingResumePrompt, runnerOnline, agentId, urlConvId]);

  // Opened when the user tries to interact with an unreachable session
  // (host offline, or not host-bound with the runner down).
  const [reconnectDialogOpen, setReconnectDialogOpen] = useState(false);

  // Pending elicitation = parked on user input — suppress shimmer. Must
  // sit before the early-return guards below (Rules of Hooks).
  const hasPendingElicitation = useMemo(
    () => blocks.some((b) => b.type === "elicitation" && b.status === "pending"),
    [blocks],
  );

  // Single-session snapshot (shared cache with chatStore.bindStream).
  // Must be declared BEFORE the early-return guards below — otherwise
  // the hook is skipped on renders that hit the loading/error branches,
  // tripping React's "rendered fewer hooks than expected".
  const { session: activeSession, isLoading: sessionLoading } = useSession(urlConvId ?? null);

  // Hoisted above the guards; the turn-start/turn-end ["session", id] invalidations refresh it (no new polling).
  const activeSessionLabels = activeSession?.labels;
  const costRoutingVerdict = useMemo(
    () => parseCostRoutingVerdict(activeSessionLabels),
    [activeSessionLabels],
  );
  // Orchestrator-only: polly's children inherit its agentName, so the gate
  // needs the session predicate (parent linkage), not a bare name check.
  const serverInfo = useServerInfo();
  const costRoutingEligible =
    serverInfo !== "loading" &&
    serverInfo.smart_routing_enabled &&
    isCostRoutingSession(activeSession);

  // Non-null only when the active session is a sub-agent (child): the
  // composer then peeks a "Chatting with sub-agent …" tray and the
  // scroll-pinned "Working…" tab is suppressed (the tray owns that slot).
  const subAgentLabel = subAgentComposerLabel(activeSession);

  // Hoisted above the early-return guards so the title-update effect can read them.
  const activeConv = urlConvId ? conversations?.find((c) => c.id === urlConvId) : null;

  // `isWorking` gates the parent's OWN turn (Stop/Interrupt) and must NOT
  // include child-session activity. `showsWorking` is display-only (tab title
  // + shimmer/pill) for the main chat and is suppressed mid-elicitation or
  // when the runner is known offline.
  const isWorking = !hasPendingElicitation && computeIsWorking(sessionStatus);
  const showsWorking = computeShowsWorking(sessionStatus, {
    hasPendingElicitation,
    runnerOnline,
    backgroundTaskCount,
  });

  // A fork of a coding session carries the source id in this label (set by
  // fork_conversation). It is provenance — it persists after the clone is
  // bound — so it identifies the source (for the picker's prefill) but is
  // NOT sufficient to decide whether to OPEN the picker. Prefer the
  // snapshot's labels, falling back to the sidebar row.
  const forkSourceId =
    activeSession?.labels?.["omnigent.fork.source_id"] ??
    activeConv?.labels?.["omnigent.fork.source_id"] ??
    null;
  // Only an *unbound* fork (no workspace yet) routes the offline guard to
  // the directory picker — which binds + launches. A bound fork that is
  // merely offline gets the CLI reconnect dialog like any other session;
  // opening the picker for it would 400 ("already has a runner bound").
  // Mirrors the server's needs_workspace flag (fork label + workspace NULL).
  const isUnboundFork = isUnboundCodingFork({
    forkSourceId,
    workspace: activeSession?.workspace ?? activeConv?.workspace ?? null,
  });

  // Author labels show only once a session is shared. A non-owner viewer
  // already implies a share; the owner needs the grant list (manage-only,
  // which the owner can read) to know they granted access to anyone else.
  // Hooks stay above the early-return guards (rules-of-hooks).
  const viewerId = getCurrentAuthorId();
  const sessionOwner = activeConv?.owner ?? null;
  const viewerOwnsSession = sessionOwner !== null && sessionOwner === viewerId;
  const { data: ownerGrants } = usePermissions(viewerOwnsSession ? (urlConvId ?? null) : null);
  const isSessionShared = isSessionSharedWithOthers(sessionOwner, viewerId, ownerGrants);

  // The open session's derived liveness — the single signal the chat
  // surface switches on to pick the right affordance (normal chat, a
  // non-blocking "wake the runner" hint, or the reconnect
  // dialog). See `useSessionLiveness`. `runnerOnline` above is still read
  // directly for terminal-view gating (the PTY is dead the moment the
  // runner tunnel drops, independent of host state).
  // `turnActive` (the chat-level status is "streaming" the instant a send
  // is dispatched) upgrades an asleep-but-host-up session to `starting`:
  // sending to a stopped runner relaunches it, and the user should see the
  // same "Connecting…" intermediate as a fresh launch rather than a gap.
  //
  // Fall back to the single-session snapshot when the sidebar row is absent
  // (a directly-opened `/c/:id`, a child/sub-agent, or an off-page session)
  // so `host_id` still reaches the hook — otherwise a host-bound, host-down
  // session misclassifies as `local_stranded` and shows the wrong reconnect
  // path. See `livenessRowFromSession`.
  //
  // Always source `host_resumable` from the session snapshot — the sidebar
  // `Conversation` row doesn't carry it. activeSession is loaded for the open
  // session, so a host-bound, host-down session whose host is a resumable
  // managed host classifies as `host_asleep` (composer open, send wakes it)
  // instead of dead-ending on `host_offline`.
  const livenessRow: LivenessRow | null = activeConv
    ? { ...activeConv, host_resumable: activeSession?.hostResumable ?? false }
    : livenessRowFromSession(activeSession);
  const liveness = useSessionLiveness(urlConvId ?? undefined, livenessRow, {
    turnActive: status === "streaming",
  });

  // Browser tab title: "● Title" while the main session is working so
  // background tabs signal parent activity without duplicating child-session
  // badges from the sidebar/Agents rail. An open-but-untitled session
  // (no synthesized title yet) reads as "New session" to match its
  // sidebar row; the landing page (no active session) stays "Omnigent".
  // Sub-agent (child) sessions are absent from the sidebar list, so
  // ``activeConv`` is null and the title would otherwise read "New session";
  // name the tab after the sub-agent instead, mirroring the header.
  const subAgentTabTitle =
    activeSession?.parentSessionId != null
      ? (boundAgentBySession?.name ?? boundAgentName ?? subAgentLabel ?? null)
      : null;
  useEffect(() => {
    const fallback = urlConvId ? UNTITLED_CONVERSATION_LABEL : "Omnigent";
    const base = truncateTitle(activeConv?.title ?? subAgentTabTitle ?? fallback);
    document.title = showsWorking ? `● ${base}` : base;
  }, [activeConv?.title, subAgentTabTitle, showsWorking, urlConvId]);

  const codexModelOptions = useChatStore((s) => s.codexModelOptions);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const llmModel = useChatStore((s) => s.llmModel);

  // Loading + error gates for `/c/:id` hydration.
  if (urlConvId) {
    if (loadingConversation) return <HydratingPlaceholder />;
    if (conversationLoadError) {
      return <ConversationLoadError conversationId={urlConvId} error={conversationLoadError} />;
    }
  }

  // The session is unreachable and a message can't wake it: the host is
  // offline (host-bound) or it isn't host-bound and the runner is down.
  // `runner_asleep` is deliberately NOT here — there the host relaunches
  // the runner on the next message, so the send must go through.
  // An in-flight managed-sandbox launch also looks unreachable to
  // liveness (no host bound yet) but is the opposite: the server parks
  // the next message on the launch rendezvous and forwards it once the
  // sandbox is up, so the send must go through. A FAILED launch keeps
  // normal unreachable handling.
  const isUnreachable =
    !sandboxLaunching && (liveness.kind === "host_offline" || liveness.kind === "local_stranded");

  function onSend(text: string, files?: File[]) {
    if (!agentId) return;
    // An unbound coding clone (fork-source label) needs a directory before
    // it can run: open the picker and stash this message to replay after
    // the bind. Pin the prompt to THIS session so it replays here, never
    // into a session the user may switch to first; carry any attachments
    // so the replay sends the same payload.
    if (urlConvId && runnerOnline === false && isUnboundFork) {
      setPendingResumePrompt({ sessionId: urlConvId, text, files: files ?? [] });
      setResumeDirDialogOpen(true);
      return;
    }
    // Unreachable → no executor to dispatch this turn to, and no host to
    // wake. Surface the reconnect dialog instead of POSTing into
    // a void.
    if (urlConvId && isUnreachable) {
      setReconnectDialogOpen(true);
      return;
    }
    // Queue instead of POSTing now (see shouldQueueSend). enqueueMessage flushes
    // FIFO immediately when genuinely idle, so nothing stalls.
    const chat = useChatStore.getState();
    if (
      shouldQueueSend(chat.conversationId, chat.status, chat.sessionStatus, chat.queuedMessages)
    ) {
      chat.enqueueMessage(text, files);
      return;
    }
    void useChatStore.getState().send(text, agentId, files, {
      onConversationCreated: (newId) => {
        // Eager URL update: the moment the server tells us this
        // conversation's id, promote `/` → `/c/:newId`. Replace (not
        // push) so the back button takes the user wherever they came
        // from rather than to a stale `/`.
        navigate(`/c/${newId}`, { replace: true });
      },
    });
  }

  function onSendSlashCommand(name: string, args: string) {
    if (!agentId) return;
    // Slash commands aren't replayed (an edge), but still route an unbound
    // coding clone to the directory picker so it isn't a dead end.
    if (urlConvId && runnerOnline === false && isUnboundFork) {
      setResumeDirDialogOpen(true);
      return;
    }
    if (urlConvId && isUnreachable) {
      setReconnectDialogOpen(true);
      return;
    }
    void useChatStore.getState().sendSlashCommand(name, args, agentId, {
      onConversationCreated: (newId) => {
        navigate(`/c/${newId}`, { replace: true });
      },
    });
  }

  function onStop() {
    useChatStore.getState().stop();
  }

  // Sub-agent (child) sessions aren't returned by the sidebar list, so
  // ``activeConv`` is null for them — the snapshot (fetched above as
  // ``activeSession``) is the only place we can learn the user's
  // effective permission level for a child.
  const permissionLevel = derivePermissionLevel(
    activeSession,
    sessionLoading,
    activeConv,
    urlConvId,
    conversationsData !== undefined,
  );
  const readOnlyReason = readOnlyReasonForSessionLabels(activeSession, activeConv);
  // Once present, the live session snapshot is authoritative.
  const capabilitySource = {
    labels: activeSession ? (activeSession.labels ?? {}) : (activeConv?.labels ?? {}),
  };
  const modelPickerKind = modelPickerKindForConv(capabilitySource);
  const effortLevels = effortLevelsForConv(
    capabilitySource,
    codexModelOptions,
    selectedModel ?? llmModel,
  );
  const showEffort = shouldShowEffortPicker(capabilitySource) && effortLevels.length > 0;

  // When inside a session, only show the bound agent — the session is
  // tied 1:1 to its runner and can't be reassigned. Show all agents on
  // `/` (no active session) so the picker still works for future CLI-
  // started sessions.
  // Prefer the full agent object (with mcp_servers) from the session
  // endpoint when viewing a conversation. Fall back to the sessions-
  // derived list for the `/` (no session) picker view.
  const visibleAgents = boundAgentId
    ? boundAgentBySession
      ? [boundAgentBySession]
      : boundAgentName
        ? [{ id: boundAgentId, name: boundAgentName } as Agent]
        : agents?.filter((a) => a.id === boundAgentId)
    : agents;

  const mainAgent = (
    <MainAgentSurface
      conversationId={urlConvId ?? null}
      bubbles={bubbles}
      status={status}
      isWorking={isWorking}
      showsWorking={showsWorking}
      runnerOnline={runnerOnline}
      liveness={liveness}
      agentsError={agentsError}
      disabled={!agentId || agentsError !== null}
      onSend={onSend}
      onSendSlashCommand={onSendSlashCommand}
      onStop={onStop}
      onShowReconnectHelp={() => {
        // Route the banner to the SAME dialog typing a message would: an
        // unbound coding clone opens the directory picker (bind + launch),
        // everything else gets the reconnect dialog.
        if (isUnboundFork) setResumeDirDialogOpen(true);
        else setReconnectDialogOpen(true);
      }}
      agents={visibleAgents}
      agentsLoading={agentsLoading}
      selectedAgentId={agentId}
      onSelectAgent={setSelectedAgentId}
      hasMoreHistory={hasMoreHistory}
      loadingMoreHistory={loadingMoreHistory}
      permissionLevel={permissionLevel}
      readOnlyReason={readOnlyReason}
      effortLevels={effortLevels}
      showEffort={showEffort}
      showModels={modelPickerKind !== null}
      modelPickerKind={modelPickerKind}
      codexModelOptions={codexModelOptions}
      showCodexPlanMode={shouldShowCodexPlanModeControl(capabilitySource)}
      showCodexGoal={shouldShowCodexGoalControl(capabilitySource)}
      costRoutingVerdict={costRoutingVerdict}
      costRoutingEligible={costRoutingEligible}
      subAgentLabel={subAgentLabel}
    />
  );

  // On `/` (no conversation), the composer would let the user POST a
  // first message and silently create a session — but sessions are
  // bound 1:1 to a local runner that only the CLI can launch. Show
  // the CLI instructions instead so users learn the right entry point.
  if (!urlConvId) return <NewChatLandingScreen />;

  // Pick the reconnect dialog's state from liveness. The dialog only
  // opens for the two unreachable variants; `host_offline` carries
  // ownership (a non-owner can't reach the host machine). Any other
  // liveness defaults to `local_stranded` — harmless since the dialog
  // stays closed unless an unreachable interaction opened it.
  const reconnectState = liveness.kind === "host_offline" ? "host_offline" : "local_stranded";
  const reconnectIsOwner = liveness.kind === "host_offline" ? liveness.isOwner : true;

  return (
    <SessionSharedContext.Provider value={isSessionShared}>
      <SessionLayout mainAgent={mainAgent} />
      <ReconnectSessionDialog
        open={reconnectDialogOpen}
        onOpenChange={setReconnectDialogOpen}
        conversationId={urlConvId}
        serverUrl={getCliServerUrl()}
        wrapper={activeConv?.labels?.["omnigent.wrapper"]}
        state={reconnectState}
        isOwner={reconnectIsOwner}
        // Source prefill for the Clone tab's fork form. Mirrors AppShell's
        // ForkSessionDialog wiring; the title additionally falls back to the
        // sidebar row, which ChatPage has at hand.
        sourceTitle={activeConv?.title ?? activeSession?.title}
        sourceWorkspace={activeSession?.workspace}
        sourceHostId={activeSession?.hostId}
        sourceGitBranch={activeSession?.gitBranch}
      />
      {isUnboundFork && forkSourceId && (
        <ResumeWithDirectoryDialog
          open={resumeDirDialogOpen}
          onOpenChange={setResumeDirDialogOpen}
          sessionId={urlConvId}
          sourceSessionId={forkSourceId}
          serverUrl={getCliServerUrl()}
          wrapper={activeConv?.labels?.["omnigent.wrapper"]}
        />
      )}
    </SessionSharedContext.Provider>
  );
}

interface SessionLayoutProps {
  mainAgent: React.ReactNode;
}

/**
 * Inside a conversation: wraps the chat surface. The terminals panel
 * and right rail are managed by AppShell and rendered outside this
 * component as flex siblings.
 */
function SessionLayout({ mainAgent }: SessionLayoutProps) {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      <div className="flex min-w-0 flex-1 flex-col">{mainAgent}</div>
    </div>
  );
}

function SelectionPopup({
  containerRef,
  onReply,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  onReply: (text: string) => void;
}) {
  const [popupPos, setPopupPos] = useState<{ x: number; y: number } | null>(null);
  const selectedTextRef = useRef<string>("");

  const updatePopup = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const text = sel.toString().trim();
    if (!text) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    // Scope to the conversation container — ignore selections in the composer.
    const container = containerRef.current;
    if (!container) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }
    const anchor = sel.anchorNode;
    if (!anchor || !container.contains(anchor)) {
      setPopupPos(null);
      selectedTextRef.current = "";
      return;
    }

    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    // Position the button just above the selection, horizontally centered.
    setPopupPos({
      x: rect.left + rect.width / 2,
      y: rect.top,
    });
    selectedTextRef.current = text;
  }, [containerRef]);

  useEffect(() => {
    document.addEventListener("mouseup", updatePopup);
    document.addEventListener("selectionchange", updatePopup);
    return () => {
      document.removeEventListener("mouseup", updatePopup);
      document.removeEventListener("selectionchange", updatePopup);
    };
  }, [updatePopup]);

  if (!popupPos) return null;

  return (
    <div
      style={{
        position: "fixed",
        // Translate left by 50% to center the button over the midpoint of the
        // selection, and up by 100% + 6px to sit just above the selection rect.
        left: popupPos.x,
        top: popupPos.y,
        transform: "translate(-50%, calc(-100% - 6px))",
        zIndex: 50,
      }}
    >
      <Button
        type="button"
        variant="secondary"
        size="sm"
        // Override shared-variant translucent hover — this button floats over text.
        className="gap-1 shadow-md hover:bg-secondary hover:brightness-95 dark:hover:brightness-110"
        onMouseDown={(e) => {
          // Prevent the mousedown from clearing the selection before we read it.
          e.preventDefault();
        }}
        onClick={() => {
          const text = selectedTextRef.current;
          if (text) {
            onReply(text);
            window.getSelection()?.removeAllRanges();
            setPopupPos(null);
            selectedTextRef.current = "";
          }
        }}
      >
        <CornerUpLeftIcon className="size-3.5" />
        Reply ↵
      </Button>
    </div>
  );
}

interface MainAgentSurfaceProps {
  /**
   * Active conversation id, or null when on the landing page. Forwarded
   * to MainTerminalView so the inline terminal can target the right
   * session in terminal-first mode.
   */
  conversationId: string | null;
  bubbles: Bubble[];
  status: "idle" | "streaming";
  /** Local stream OR cross-client `session.status: running`. Gates the
   *  composer's Stop/Interrupt button — the parent's OWN turn only. */
  isWorking: boolean;
  /** Display-only main-chat indicator after elicitation/offline gates.
   *  Never includes child-session activity and never gates Stop/Interrupt. */
  showsWorking: boolean;
  /**
   * Strict runner-tunnel liveness, used only to gate the inline terminal
   * view (the PTY dies the moment the runner tunnel drops). The reconnect
   * affordances key off `liveness` instead.
   */
  runnerOnline: boolean | undefined;
  /** Derived open-session liveness — drives the reconnect hint/banner. */
  liveness: SessionLiveness;
  agentsError: unknown;
  disabled: boolean;
  onSend: (text: string, files?: File[]) => void;
  /**
   * Invoke a skill via the `slash_command` event path. Gated off inside
   * `MainAgentSurface` for terminal-first (native) sessions, where `/skill`
   * is sent as plaintext for the vendor TUI to handle. See
   * `ComposerProps.onSendSlashCommand`.
   */
  onSendSlashCommand?: (name: string, args: string) => void;
  onStop: () => void;
  onShowReconnectHelp: () => void;
  agents: Agent[] | undefined;
  agentsLoading: boolean;
  selectedAgentId: string | null;
  onSelectAgent: (id: string) => void;
  /** Whether older messages exist that haven't been loaded yet. */
  hasMoreHistory: boolean;
  /** Whether a load-more fetch is currently in flight. */
  loadingMoreHistory: boolean;
  permissionLevel: number | null;
  /** Forces composer read-only with the given placeholder when non-null. See ``ComposerProps.readOnlyReason``. */
  readOnlyReason: string | null;
  effortLevels: readonly string[];
  /** Show effort controls. */
  showEffort: boolean;
  /** Whether the picker dropdown should include a Models section. */
  showModels: boolean;
  /** Native model picker family, when present. */
  modelPickerKind: NativeModelPickerKind | null;
  /** Codex app-server model options for codex-native sessions. */
  codexModelOptions: readonly CodexModelOption[];
  /** Show the Codex Plan-mode toggle. */
  showCodexPlanMode: boolean;
  /** Show the Codex Goal control. */
  showCodexGoal?: boolean;
  /** Latest advisor verdict for the cost-routing pill; null when none. */
  costRoutingVerdict: CostRoutingVerdict | null;
  /** Session passes `isCostRoutingSession` (polly orchestrator, not a child). */
  costRoutingEligible: boolean;
  /**
   * Sub-agent instance label when the active session is a child, e.g.
   * ``"check-account-eligibility"``; ``null`` for top-level sessions.
   * Drives the composer's "Chatting with sub-agent …" tray and suppresses
   * the scroll-pinned "Working…" tab (the tray takes that slot). See
   * ``subAgentComposerLabel``.
   */
  subAgentLabel: string | null;
}

/**
 * Whether terminal-first sessions should replace chat with the inline
 * terminal surface. Runner health is intentionally ignored: an offline
 * stopped/resumable session still needs the empty terminal page so the
 * user can resume from there.
 */
export function shouldShowTerminalSurface(
  conversationId: string | null,
  terminalFirst:
    | {
        isTerminalFirst: boolean;
        view: "chat" | "terminal";
      }
    | null
    | undefined,
  _runnerOnline: boolean | undefined,
): boolean {
  return (
    !!conversationId && terminalFirst?.isTerminalFirst === true && terminalFirst.view === "terminal"
  );
}

/**
 * The conversation scroll surface + composer — the content of the
 * "Main Agent" tab (and also the standalone view on `/`).
 *
 * In terminal-first sessions, when the connection pill is set to
 * Terminal, the conversation + composer are replaced by an inline
 * `MainTerminalView`. The pill itself stays visible (rendered via
 * `ConnectionIndicator`) so the user can flip back to Chat.
 */
function MainAgentSurface({
  conversationId,
  bubbles,
  status,
  isWorking,
  showsWorking,
  runnerOnline,
  liveness,
  agentsError,
  disabled,
  onSend,
  onSendSlashCommand,
  onStop,
  onShowReconnectHelp,
  agents,
  agentsLoading,
  selectedAgentId,
  onSelectAgent,
  hasMoreHistory,
  loadingMoreHistory,
  permissionLevel,
  readOnlyReason,
  effortLevels,
  showEffort,
  showModels,
  modelPickerKind,
  codexModelOptions,
  showCodexPlanMode,
  showCodexGoal = false,
  costRoutingVerdict,
  costRoutingEligible,
  subAgentLabel,
}: MainAgentSurfaceProps) {
  const terminalFirst = useTerminalFirst();
  // Mirrors ChatPage's `sandboxLaunching`: while the managed-sandbox
  // launch runs, the composer must stay sendable — the server parks
  // the message on the launch rendezvous — even though liveness reads
  // the not-yet-host-bound session as stranded.
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  const sandboxLaunching = sandboxStatus !== null && sandboxStatus.stage !== "failed";
  // Render the inline terminal whenever the user has opted in via the
  // connection pill. The terminal surface owns its no-terminal state,
  // including stopped/resumable sessions, and the connection indicator
  // remains below it for offline sessions.
  const showTerminal = shouldShowTerminalSurface(conversationId, terminalFirst, runnerOnline);

  // All hook calls below must run on every render regardless of
  // `showTerminal` — Rules of Hooks. The early return for the terminal
  // branch lives below, after every hook has run.

  // Single nav instance shared by hotkey + buttons (see useUserMessageNav).
  // System-message bubbles (`[System: ...]` notifications rendered via
  // SystemMessageView) are excluded — the hotkey is for navigating real
  // user turns, not runtime markers.
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

  // Pending elicitation cards float to the bottom of the chat: rendered as the
  // last items in the scroll flow and removed from their inline position so
  // they don't render twice. Stick-to-bottom then keeps an outstanding
  // question in view instead of letting trailing text scroll it off the top.
  // Answered cards stay inline at their natural spot. `streamBubbles` keeps
  // `bubbles`' reference when nothing is pending, so the common case allocates
  // nothing.
  const pendingElicitations = useMemo(() => collectPendingElicitations(bubbles), [bubbles]);
  const streamBubbles = useMemo(
    () => (pendingElicitations.length === 0 ? bubbles : stripPendingElicitations(bubbles)),
    [bubbles, pendingElicitations.length],
  );

  // Cmd+Alt+↑/↓ (Ctrl+Alt on win/linux) — guarded so the composer's
  // own unmodified ArrowUp/Down history-recall still works.
  useEffect(() => {
    // globalThis prefix because React's KeyboardEvent is imported above.
    const handler = (e: globalThis.KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || !e.altKey) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      const target = e.target;
      if (
        target instanceof HTMLElement &&
        target.closest('textarea, input, [contenteditable="true"]')
      ) {
        return;
      }
      e.preventDefault();
      if (e.key === "ArrowUp") nav.goPrev();
      else nav.goNext();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [nav]);

  // Active reply quotes — each "Reply ↵" click appends; consumed by Composer.
  const [replyQuotes, setReplyQuotes] = useState<string[]>([]);

  // Ref forwarded to SelectionPopup to scope selection detection to the
  // conversation area, preventing selections in the composer from triggering
  // the popup. Mirrored into state (`containerEl`) so JumpToTopButton — which
  // renders inside this wrapper, outside the mask-faded scroll viewport — can
  // attach its hover listeners to the wrapper (the common ancestor of both the
  // scroll area and the pill, so moving the cursor onto the pill keeps it live).
  const conversationRef = useRef<HTMLElement | null>(null);
  const [containerEl, setContainerEl] = useState<HTMLElement | null>(null);
  const setConversationEl = useCallback((el: HTMLDivElement | null) => {
    conversationRef.current = el;
    setContainerEl(el);
  }, []);
  const [terminalSurfaceEl, setTerminalSurfaceEl] = useState<HTMLElement | null>(null);
  // True only while the chat/terminal surface is the frontmost thing on screen.
  // Drives both native overlays so neither floats over an opened drawer.
  const surfaceFrontmost = useSurfaceFrontmost(
    showTerminal ? terminalSurfaceEl : containerEl,
    !!conversationId,
  );
  useEffect(() => {
    if (!isIOSShell()) return;
    setNativeServerSwitcherHidden(!surfaceFrontmost);
  }, [surfaceFrontmost]);
  useEffect(() => {
    if (!isIOSShell()) return;
    return () => setNativeServerSwitcherHidden(true);
  }, []);
  // The conversation's scroll container + the StickToBottom controls needed to
  // override its bottom-lock, lifted out of the context by
  // ConversationScrollRefBridge so the pinned-but-unmasked JumpToTopButton can
  // read and drive the scroll.
  const [scroller, setScroller] = useState<ConversationScroller | null>(null);
  // While the iOS edge-swipe is driving the sidebar drawer, make the transcript
  // ignore the finger so it doesn't scroll along with the drag. On iOS the page
  // is viewport-locked, so the transcript scrolls as an inner overflow:auto
  // element (`scroller.el`) that the native shell can't reach via
  // webView.scrollView — it has to be frozen here in the DOM. The native drag
  // stream marks when a drag is live; for its duration the scroller stops
  // responding to touch (pointer-events:none), its overflow is locked, and its
  // scroll offset is pinned so neither a finger-drag nor leftover momentum can
  // move it. Everything is restored when the drag settles (open/close).
  useEffect(() => {
    const el = scroller?.el;
    if (!el) return;
    let frozenTop: number | null = null;
    const pin = () => {
      if (frozenTop != null) el.scrollTop = frozenTop;
    };
    const freeze = () => {
      if (frozenTop != null) return;
      frozenTop = el.scrollTop;
      el.style.pointerEvents = "none";
      el.style.overflowY = "hidden";
      el.addEventListener("scroll", pin);
    };
    const thaw = () => {
      if (frozenTop == null) return;
      el.removeEventListener("scroll", pin);
      el.style.pointerEvents = "";
      el.style.overflowY = "auto";
      frozenTop = null;
    };
    const unsubscribe = onNativeSidebarDrag((phase) => {
      if (phase === "begin" || phase === "move") freeze();
      else thaw();
    });
    return () => {
      unsubscribe();
      thaw();
    };
  }, [scroller]);
  const [sendScrollNonce, setSendScrollNonce] = useState(0);
  const handleSend = useCallback(
    (text: string, files?: File[]) => {
      setSendScrollNonce((n) => n + 1);
      onSend(text, files);
    },
    [onSend],
  );
  // Wrap the slash-command sender the same way (scroll to bottom on send).
  // Gated off for native-wrapper sessions (claude-native / codex-native):
  // there the composer's `/skill` must reach the vendor TUI as plaintext
  // (the server has no slash_command path for native sessions). Undefined
  // → the composer falls through to the plaintext send for these. Keyed
  // on the wrapper label, NOT `isTerminalFirst` — a terminal-first SDK
  // session (embedded Omnigent REPL terminal) runs an in-process harness
  // with the full server-side slash_command path.
  const isTerminalFirst = terminalFirst?.isTerminalFirst === true;
  const isNativeWrapper = terminalFirst?.isNativeWrapper === true;
  const handleSendSlashCommand = useMemo(
    () =>
      onSendSlashCommand && !isNativeWrapper
        ? (name: string, args: string) => {
            setSendScrollNonce((n) => n + 1);
            onSendSlashCommand(name, args);
          }
        : undefined,
    [onSendSlashCommand, isNativeWrapper],
  );

  // "Working…" stays lit for the whole busy turn — through streaming text,
  // tool runs, and reasoning gaps — including after a reload that hydrates
  // `running` before any bubbles exist locally. Only a trailing compaction
  // spinner suppresses it (that bubble owns the slot with its own animation).
  const showWorkingIndicator = shouldShowWorkingIndicator(showsWorking, bubbles);

  if (showTerminal && conversationId) {
    return (
      <>
        <MainTerminalView
          conversationId={conversationId}
          initialTerminalKey={terminalFirst?.terminalViewKey}
          onSurfaceElement={setTerminalSurfaceEl}
          // Non-owners attach read-only: a shared PTY can't attribute
          // input per-user, so only the owner may type. They drive the
          // agent via the composer instead. Server enforces this too.
          readOnly={!isOwnerLevel(permissionLevel)}
        />
        <ConnectionIndicator
          liveness={liveness}
          onShowReconnectHelp={onShowReconnectHelp}
          surfaceFrontmost={surfaceFrontmost}
        />
      </>
    );
  }

  return (
    <>
      {/* Wrapper div gives us a ref to scope the SelectionPopup to the
          conversation area without requiring Conversation to forward refs. */}
      <div ref={setConversationEl} className="relative flex min-h-0 flex-1 overflow-hidden">
        {/* chat-scroll-fade masks the viewport's top edge so scrolling
            content dissolves into the canvas before reaching the
            ChatHeader overlay's controls (geometry in index.css). */}
        <Conversation className="chat-scroll-fade flex-1">
          {/* gap-4 overrides ConversationContent's default gap-8 so consecutive agent turns read as one thread. */}
          <ConversationContent
            className={cn(
              "chat-conversation-content mx-auto w-full gap-4 pt-20 pb-6",
              CHAT_COLUMN_WIDTH,
            )}
          >
            {/* Scroll helpers — must live inside StickToBottom to access context. */}
            <ScrollToBottomOnSend nonce={sendScrollNonce} />
            <PreserveScrollDistanceOnResize />
            <ConversationScrollRefBridge onScroller={setScroller} />
            <HistoryAutoLoader
              hasMoreHistory={hasMoreHistory}
              loadingMoreHistory={loadingMoreHistory}
            />
            {bubbles.length === 0 && !showWorkingIndicator ? (
              // Cold launch: a centered spinner instead of the "ready to
              // type" empty state (the create-then-send path uses the
              // "row" variant). Two launch shapes land here: a
              // terminal-first spin-up (gate on isTerminalFirst too —
              // terminalStartingUp is set for non-terminal-first sessions
              // as well) and a managed-sandbox launch, whose stage text
              // renders in the same spot for ANY session type.
              (terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp) ||
              sandboxLaunching ? (
                <RunnerStartingIndicator variant="hero" />
              ) : (
                <ConversationEmptyState>
                  <div className="space-y-1.5">
                    <h3 className="text-2xl font-medium tracking-[-0.02em]">
                      What should we work on?
                    </h3>
                    <p className="text-muted-foreground text-base">
                      {agentsError
                        ? `Failed to load agents: ${agentsError instanceof Error ? agentsError.message : String(agentsError)}`
                        : "Send a message to get started."}
                    </p>
                  </div>
                </ConversationEmptyState>
              )
            ) : (
              <>
                {streamBubbles.map((bubble) => (
                  <BubbleView key={bubbleKey(bubble)} bubble={bubble} />
                ))}
                {/* Pending elicitation cards, floated to the bottom of the
                    chat so an outstanding question stays in view (stick-to-
                    bottom) no matter how much text the agent streamed after
                    it. Wrapped in an assistant Message so each matches an
                    inline card's look; removed from their inline slot by
                    `stripPendingElicitations`. Newest renders last, nearest
                    the composer. Rendered ABOVE the Working… indicator so the
                    card sits closest to the prompt and the shimmer stays the
                    last thing in the flow. */}
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
                {/* Working… shimmer, lit for the whole busy turn so the user
                    always sees the session is still going. Suppressed when the
                    last bubble is a compaction spinner — that bubble already
                    owns the "in-progress" slot. aria-hidden: the pinned pill
                    owns the single aria-live region (see WorkingStatusPin). */}
                {showWorkingIndicator && <WorkingIndicator />}
                {/* Terminal-first spin-up cue beneath the just-sent first
                    message: the prompt bubble renders immediately (no
                    runner-online send gate), but `showWorkingIndicator` stays
                    suppressed while the runner is offline, so without this the
                    user's message sits with no sign anything is happening.
                    Self-gates to null off the spin-up window; rendered only
                    when not already showing Working… so the two never stack. */}
                {!showWorkingIndicator && <RunnerStartingIndicator variant="row" />}
              </>
            )}
          </ConversationContent>
          <ConversationScrollButton />
          {/* Outside ConversationContent so it's pinned to the viewport, not the scroll. See WorkingStatusPin.
              Suppressed in a sub-agent session: the composer's "Chatting with sub-agent …" tray owns this slot. */}
          <WorkingStatusPin show={showWorkingIndicator} suppress={subAgentLabel != null} />
          <UserMessageNavConnected
            goPrev={nav.goPrev}
            goNext={nav.goNext}
            canPrev={nav.canPrev}
            canNext={nav.canNext}
            hidden={userMessageIds.length === 0}
          />
        </Conversation>
        {/* Hover the top edge to reveal a pill that loads all older history and
            scrolls to the first message. Rendered here (a wrapper sibling of
            Conversation) rather than inside it so it escapes the chat-scroll-fade
            mask and can sit right at the fade border. */}
        <JumpToTopButton
          containerEl={containerEl}
          scroller={scroller}
          hasMoreHistory={hasMoreHistory}
        />
      </div>
      {/* Floating reply button — scoped to the conversation container. */}
      <SelectionPopup
        containerRef={conversationRef}
        onReply={(text) => setReplyQuotes((prev) => [...prev, text])}
      />

      <Composer
        disabled={disabled}
        status={status}
        isWorking={isWorking}
        onSend={handleSend}
        onSendSlashCommand={handleSendSlashCommand}
        onStop={onStop}
        agents={agents}
        agentsLoading={agentsLoading}
        selectedAgentId={selectedAgentId}
        onSelectAgent={onSelectAgent}
        permissionLevel={permissionLevel}
        readOnlyReason={readOnlyReason}
        replyQuotes={replyQuotes}
        onRemoveQuote={(i) => setReplyQuotes((prev) => prev.filter((_, idx) => idx !== i))}
        onClearAllQuotes={() => setReplyQuotes([])}
        effortLevels={effortLevels}
        showEffort={showEffort}
        showModels={showModels}
        modelPickerKind={modelPickerKind}
        codexModelOptions={codexModelOptions}
        showCodexPlanMode={showCodexPlanMode}
        showCodexGoal={showCodexGoal}
        isTerminalFirst={isTerminalFirst}
        isNativeWrapper={isNativeWrapper}
        reconnectHint={liveness.kind === "runner_asleep" || liveness.kind === "host_asleep"}
        sandboxAsleepHint={liveness.kind === "host_asleep"}
        unreachable={
          !sandboxLaunching &&
          (liveness.kind === "host_offline" || liveness.kind === "local_stranded")
        }
        costRoutingVerdict={costRoutingVerdict}
        costRoutingEligible={costRoutingEligible}
        subAgentLabel={subAgentLabel}
      />

      {/* Chat/Terminal toggle for terminal-first sessions, reconnect-or-
          fork banner when unreachable, nothing otherwise. Sits below the
          composer so its position is consistent with the terminal view. */}
      <ConnectionIndicator
        liveness={liveness}
        onShowReconnectHelp={onShowReconnectHelp}
        surfaceFrontmost={surfaceFrontmost}
      />
    </>
  );
}

function HydratingPlaceholder() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 text-muted-foreground text-sm">
      <Loader2Icon className="size-4 animate-spin" />
      Loading conversation…
    </div>
  );
}

/**
 * Error state for `/c/:id` when the items endpoint fails. Shown
 * verbatim instead of falling through to the chat surface so the user
 * sees the problem (instead of a blank chat that silently posts to a
 * non-existent conversation on next send). Most common cause: invalid
 * conversation id in the URL — surfaces quickly because the store's
 * items fetch disables retries.
 */
function ConversationLoadError({
  conversationId,
  error,
}: {
  conversationId: string;
  error: Error;
}) {
  const navigate = useNavigate();
  return (
    <div className="flex flex-1 items-center justify-center px-6">
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <h1 className="font-medium text-foreground text-lg">Conversation not found</h1>
        <p className="text-muted-foreground text-sm">
          Couldn't load{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{conversationId}</code>
          : {error.message}
        </p>
        {/* Route to the home composer ("/"), which owns session creation. */}
        <Button type="button" variant="outline" onClick={() => navigate("/")}>
          Start a new chat
        </Button>
      </div>
    </div>
  );
}

/**
 * Adds scroll-state CSS classes to UserMessageNav. The responsive behavior
 * itself stays in Tailwind classes: hidden below `md` only while pinned to
 * the bottom, visible again as soon as the user scrolls up.
 */
function UserMessageNavConnected(props: React.ComponentProps<typeof UserMessageNav>) {
  const { isAtBottom } = useStickToBottomContext();
  return (
    <UserMessageNav {...props} className={cn(props.className, isAtBottom && "max-md:hidden")} />
  );
}

/**
 * Scroll-pinned "Working…" pill — sole aria-live region (inline shimmer is
 * aria-hidden).
 *
 * @param show - True while the main session is working; gates both the
 *   aria-live announcement and the painted tab.
 * @param suppress - Hides the painted tab without silencing the aria-live
 *   region (still gated on ``show``). Set in a sub-agent session, where the
 *   composer's "Chatting with sub-agent …" tray rises in this same slot and
 *   the "Working…" tab would otherwise stack on top of it.
 */
function WorkingStatusPin({ show, suppress = false }: { show: boolean; suppress?: boolean }) {
  const { isAtBottom } = useStickToBottomContext();
  const bgCount = useChatStore((s) => s.backgroundTaskCount);
  const tick = useWorkingLabelTick();
  const visible = show && !isAtBottom && !suppress;
  return (
    <div
      // Always mounted (the aria-live region announces on show); bottom-0 sits
      // it flush on the composer so the tab reads as rising from behind it.
      role="status"
      aria-live="polite"
      data-testid="working-indicator-pin"
      className={cn(
        "pointer-events-none absolute inset-x-0 bottom-0 z-20 transition-opacity duration-200",
        visible ? "opacity-100" : "opacity-0",
      )}
    >
      {/* The single announced string. Held stable at "Working…" so the rotating
          tab text below never re-announces every few seconds. Present whenever
          the agent is working, so it announces whether the tab is painted
          (scrolled up) or collapsed (at the bottom, where the inline shimmer
          owns the visuals). */}
      {show && <span className="sr-only">Working…</span>}
      {/* Mirror the conversation content column (mx-auto + px-6 + width) so the
          tab's left edge lines up with the inline shimmer's. */}
      <div className={cn("mx-auto w-full px-6", CHAT_COLUMN_WIDTH)}>
        {show && (
          // Tab shape (rounded top, no bottom border, composer-matching bg) so
          // its flat bottom edge merges into the chat box. aria-hidden: the
          // sr-only span above owns the announcement, so the rotating label
          // here stays silent to screen readers. Collapses to sr-only when at
          // the bottom (`!visible`) — the inline shimmer paints there instead.
          <div
            aria-hidden="true"
            className={cn(
              "flex w-fit items-center gap-1.5 rounded-t-lg border border-b-0 border-border bg-card px-3 pt-1 pb-1.5",
              !visible && "sr-only",
            )}
          >
            <OttoIcon className="otto-working h-4 w-auto shrink-0" />
            <Shimmer className="text-xs font-mono" duration={1.5}>
              {workingIndicatorLabel(bgCount, tick)}
            </Shimmer>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Forces the conversation back to the bottom when this client submits a
 * new message. StickToBottom intentionally respects a user who has scrolled
 * up while streaming, but an explicit send should bring the fresh user bubble
 * and ensuing response back into view.
 */
function ScrollToBottomOnSend({ nonce }: { nonce: number }) {
  const { scrollToBottom } = useStickToBottomContext();

  useLayoutEffect(() => {
    if (nonce === 0) return;
    scrollToBottom("instant");
    requestAnimationFrame(() => scrollToBottom("instant"));
  }, [nonce, scrollToBottom]);

  return null;
}

/**
 * Preserves the transcript's distance-from-bottom whenever its scroll container
 * resizes on the iOS shell — so the content you're looking at stays put while
 * the soft keyboard opens/closes (and while the composer grows on focus).
 *
 * Two things resize the container, and neither is handled by `use-stick-to-
 * bottom` (which only re-anchors on *content* resize): the keyboard, via
 * `useIOSViewportLock` shrinking the app-shell; and the composer growing taller
 * when focused (its send row / status line), which steals flex height from the
 * transcript a couple of lines at a time — *without* firing a visualViewport
 * resize. Watching only visualViewport missed the composer growth, which is why
 * the transcript crept up ~2 lines on focus.
 *
 * So we watch the scroll container itself with a `ResizeObserver` and, on any
 * size change, hold the scroll position relative to the bottom constant:
 * `scrollTop = scrollHeight - clientHeight - distance`. `distance` is tracked
 * from genuine user scrolls only — scrolls that coincide with a dimension change
 * (the resize's own clamp, or our restore) are ignored so they can't corrupt it.
 * At the bottom (distance 0) you stay at the bottom; scrolled up reading
 * history, you keep seeing the same messages. New messages still go through the
 * library (content resize doesn't change the container's box). Stateless across
 * any number of keyboard cycles.
 */
function PreserveScrollDistanceOnResize() {
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef?: React.RefObject<HTMLElement>;
  };
  const scrollRef = ctx.scrollRef;

  useEffect(() => {
    if (!isIOSShell()) return;
    const el = scrollRef?.current;
    if (!el) return;

    const measure = () => el.scrollHeight - el.clientHeight - el.scrollTop;
    let distance = Math.max(0, measure());
    let prevSH = el.scrollHeight;
    let prevCH = el.clientHeight;

    const onScroll = () => {
      const sh = el.scrollHeight;
      const ch = el.clientHeight;
      // A scroll that lands on the same frame as a size change is resize-induced
      // (the browser's clamp, or our own restore below) — not the user. Skip it
      // so it can't overwrite the distance we're trying to preserve.
      if (sh !== prevSH || ch !== prevCH) {
        prevSH = sh;
        prevCH = ch;
        return;
      }
      distance = Math.max(0, measure());
    };

    const observer = new ResizeObserver(() => {
      el.scrollTop = el.scrollHeight - el.clientHeight - distance;
      prevSH = el.scrollHeight;
      prevCH = el.clientHeight;
    });

    el.addEventListener("scroll", onScroll, { passive: true });
    observer.observe(el);
    return () => {
      el.removeEventListener("scroll", onScroll);
      observer.disconnect();
    };
  }, [scrollRef]);

  return null;
}

/**
 * Headless older-history loader. Pages older session items in two ways
 * with no visible control:
 *
 * 1. Near-top scroll trigger — fetches as the user scrolls toward the top.
 * 2. Viewport-fill guard — when the loaded window is too short to produce a
 *    scrollbar (so the scroll trigger can never fire), keeps paging until
 *    the content overflows or history runs out, keeping older messages
 *    reachable without a button.
 *
 * Must be rendered inside a `StickToBottom` tree to access `scrollRef`.
 *
 * @param hasMoreHistory - Whether older messages exist before the loaded window.
 * @param loadingMoreHistory - Whether an older-history page is currently loading.
 */
export function HistoryAutoLoader({
  hasMoreHistory,
  loadingMoreHistory,
}: {
  hasMoreHistory: boolean;
  loadingMoreHistory: boolean;
}) {
  // useStickToBottomContext exposes scrollRef (the actual scroll container
  // element) in the runtime context even though the public TS types only
  // declare isAtBottom and scrollToBottom. Cast to access it.
  const ctx = useStickToBottomContext() as ReturnType<typeof useStickToBottomContext> & {
    scrollRef: React.RefObject<HTMLElement>;
  };

  // Preserve scroll position when items are prepended after a scroll-up
  // fetch. Snapshot scrollHeight before the call; restore the offset in a
  // layout effect so the visible content doesn't jump.
  const prevScrollHeightRef = useRef<number | null>(null);
  const loadOlderPreservingOffset = useCallback(() => {
    if (!hasMoreHistory || loadingMoreHistory) return;
    const el = ctx.scrollRef?.current;
    if (el) prevScrollHeightRef.current = el.scrollHeight;
    void useChatStore.getState().loadMoreHistory();
  }, [ctx.scrollRef, hasMoreHistory, loadingMoreHistory]);

  useLayoutEffect(() => {
    const el = ctx.scrollRef?.current;
    // Wait until loadingMoreHistory is false — the prepend render that grows
    // scrollHeight is the one to correct. Consuming the snapshot earlier
    // would null the ref before the prepend lands, causing a scroll jump.
    if (!el || prevScrollHeightRef.current === null || loadingMoreHistory) return;
    const delta = el.scrollHeight - prevScrollHeightRef.current;
    if (delta > 0) el.scrollTop += delta;
    prevScrollHeightRef.current = null;
  });

  useEffect(() => {
    const el = ctx.scrollRef?.current;
    if (!el) return;
    const handleScroll = () => {
      if (el.scrollTop < 300 && hasMoreHistory && !loadingMoreHistory) {
        loadOlderPreservingOffset();
      }
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, [ctx.scrollRef, hasMoreHistory, loadingMoreHistory, loadOlderPreservingOffset]);

  // Viewport-fill guard. When the loaded window is too short to overflow, page
  // again so older history stays reachable without a scrollbar to scroll up.
  // No offset snapshot here: with a short window the user sits at the bottom
  // and use-stick-to-bottom keeps them pinned as older items prepend.
  const maybeFillViewport = useCallback(() => {
    const el = ctx.scrollRef?.current;
    if (!el || !hasMoreHistory || loadingMoreHistory) return;
    if (el.scrollHeight <= el.clientHeight) {
      void useChatStore.getState().loadMoreHistory();
    }
  }, [ctx.scrollRef, hasMoreHistory, loadingMoreHistory]);

  // Re-check on mount and whenever a fetch settles (loadingMoreHistory flips
  // back to false): if content still doesn't overflow, the callback pages again.
  useEffect(() => {
    maybeFillViewport();
  }, [maybeFillViewport]);

  // Re-check when the viewport grows (window resize, side panel close): a
  // previously-scrollable window can stop overflowing, removing the scrollbar
  // and stranding older history with nothing left to trigger a fetch.
  useEffect(() => {
    const el = ctx.scrollRef?.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => maybeFillViewport());
    observer.observe(el);
    return () => observer.disconnect();
  }, [ctx.scrollRef, maybeFillViewport]);

  // No visible control — history loads purely on scroll-up / viewport fill.
  return null;
}

/**
 * The conversation's scroll container plus the minimal StickToBottom controls
 * the JumpToTopButton needs to override the library's bottom-lock. `state` is a
 * stable, mutable object: clearing `isAtBottom`/`escapedFromLock` makes the
 * resize-driven `scrollToBottom({preserveScrollPosition})` — fired on every
 * history prepend — bail instead of yanking the view back to the bottom.
 */
type ConversationScroller = {
  el: HTMLElement;
  state: { isAtBottom: boolean; escapedFromLock: boolean };
  stopScroll: () => void;
};

/**
 * Lifts the StickToBottom scroll container (and lock controls) out of the
 * context so a sibling rendered *outside* `<Conversation>` (and thus outside
 * its `chat-scroll-fade` mask) can still read and drive it. `scrollRef`,
 * `state`, and `stopScroll` are stable identities (see HistoryAutoLoader for
 * the runtime-vs-types cast). Renders nothing.
 */
function ConversationScrollRefBridge({
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
 * Hover-revealed "Jump to top" pill, mirroring {@link ConversationScrollButton}
 * but for the other end. Hovering near the top edge of the conversation
 * surfaces a pill at the fade border; clicking it pages in every older history
 * block (the conversation is lazily paginated — see {@link HistoryAutoLoader})
 * and then scrolls to the very first message.
 *
 * Rendered as a sibling of `<Conversation>`, not a child: the scroll viewport's
 * top ~80px is mask-faded (`chat-scroll-fade`), so a pill inside it would fade
 * out too. Sitting in the wrapper keeps it at full opacity right at the fade
 * line, and `z-40` lifts it over the `z-30` ChatHeader so it stays clickable.
 *
 * Hover is detected in JS off the **wrapper** (`containerEl`), the common
 * ancestor of both the scroll area and this pill — listening on the scroll
 * element instead would fire `mouseleave` the instant the cursor crossed onto
 * the pill (a non-descendant), killing the click. `scroller` carries the inner
 * scroll container plus the StickToBottom lock controls.
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
  // Reveal the pill while the user is scrolling up, then fade it back out once
  // they pause — so it's reachable without having to find the top hover band.
  const [scrolledUp, setScrolledUp] = useState(false);

  // How long the pill lingers after the last upward scroll before fading out.
  const SCROLL_REVEAL_MS = 2000;

  // Pixels below the conversation's top edge that count as "hovering the top".
  // Comfortably clears the pill (anchored at the fade border, ~50px) so moving
  // onto it to click never drops the hover state.
  const HOVER_BAND_PX = 140;

  // Hover detection on the wrapper so the pill (a wrapper child) stays in-band.
  useEffect(() => {
    if (!containerEl) return;
    const onMove = (e: MouseEvent) => {
      const next = e.clientY - containerEl.getBoundingClientRect().top < HOVER_BAND_PX;
      // Only commit on a transition — mousemove fires continuously, and React
      // bails on a no-op setState anyway, but skipping it avoids the work.
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
      setAtTop((prev) => (prev === next ? prev : next));
      // Upward scroll (and not already pinned to the top): show the pill and
      // (re)arm the idle timer that fades it out once scrolling settles.
      if (top < lastTop - 1 && top > 1) {
        setScrolledUp(true);
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => setScrolledUp(false), SCROLL_REVEAL_MS);
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

  // Somewhere to go: older pages exist, or we're scrolled down within the
  // loaded window. At the very first message there's nothing to jump to.
  const canJump = hasMoreHistory || !atTop;
  const visible = jumping || ((hovering || scrolledUp) && canJump);

  const jumpToTop = useCallback(async () => {
    if (!scroller) return;
    const { el, state, stopScroll } = scroller;
    const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    setJumping(true);
    try {
      // Release StickToBottom's bottom-lock. Without this, every history prepend
      // resizes the content and the library's ResizeObserver yanks the view back
      // to the bottom (scrollToBottom with preserveScrollPosition, which sticks
      // whenever state.isAtBottom is true) — so our scrollTop=0 lost the fight
      // and only a *second* click (everything already loaded, no resizes) won.
      // Clearing the lock here makes those prepend-driven scrolls bail.
      stopScroll();
      state.isAtBottom = false;
      state.escapedFromLock = true;

      // Page in every older block before scrolling. loadMoreHistory serializes
      // via its own loadingMoreHistory guard (so a concurrent HistoryAutoLoader
      // fetch is harmless), and flips hasMoreHistory to false at the start of
      // history or on error. The rAF wait yields a frame for the prepend to
      // commit and for the in-flight flag to settle between pages. The
      // iteration cap is a backstop against a server that never reports done.
      for (let i = 0; i < 1000 && useChatStore.getState().hasMoreHistory; i++) {
        await useChatStore.getState().loadMoreHistory();
        // Keep the lock released — a prepend that briefly lands us near the
        // bottom can otherwise re-arm it via the library's scroll handler.
        state.isAtBottom = false;
        state.escapedFromLock = true;
        await nextFrame();
      }
      // Pin to the very top, re-asserting across frames until it holds. The last
      // prepends keep growing scrollHeight after the store settles, and
      // HistoryAutoLoader's offset-preservation can bump scrollTop right after
      // we zero it. Force 0 each frame until it stays 0 for two consecutive
      // frames (or we hit the frame cap).
      for (let i = 0, stable = 0; i < 60 && stable < 2; i++) {
        if (el.scrollTop === 0) stable += 1;
        else {
          el.scrollTop = 0;
          stable = 0;
        }
        await nextFrame();
      }
    } finally {
      setJumping(false);
    }
  }, [scroller]);

  return (
    <div
      // top 50px centers the pill on the chat-scroll-fade border (the mask ramps
      // 48px→80px), just below the h-14 ChatHeader. z-40 > header z-30. On the
      // iOS shell the header and fade border shift down by the safe-area inset
      // (see .chat-scroll-fade in index.css), so add --omnigent-inset-top here
      // too to keep the pill centered on the border. The var is 0px off-shell.
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
        // When hidden (opacity-0 / pointer-events-none) keep the button out of
        // the tab order and the accessibility tree so it can't take focus or be
        // announced while invisible.
        tabIndex={visible ? 0 : -1}
        aria-hidden={!visible}
        className={cn(
          "h-7 gap-1.5 rounded-full px-3 text-xs shadow-sm",
          // Force an OPAQUE background in both themes and on hover. The outline
          // variant's hover (bg-muted) is a translucent black wash (--muted is
          // #0000000f), so over the faded chat text behind the pill it bleeds
          // through and reads as transparent. bg-background is opaque (#fff /
          // #0d1218); hover feedback comes from a brightness filter, which keeps
          // the fill fully opaque.
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

/** Stable React key per bubble. */
function bubbleKey(bubble: Bubble): string {
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
 * (and every unit test) lands on. Keep each entry a single short word +
 * ellipsis — `Shimmer` scales its sweep width to the text length, so uniform
 * lengths keep the animation steady across rotations.
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
 * The label shown next to the working spinner. When background shells outlive
 * the turn (`bgCount > 0`) it names how many are still running (the tick is
 * ignored — that count is information, not decoration). Otherwise it rotates
 * through `WORKING_MESSAGES` by wall-clock `tick`.
 */
export function workingIndicatorLabel(bgCount: number, tick = 0): string {
  if (bgCount > 0) {
    return bgCount === 1
      ? "1 background task still running"
      : `${bgCount} background tasks still running`;
  }
  return WORKING_MESSAGES[tick % WORKING_MESSAGES.length]!;
}

function WorkingIndicator() {
  const bgCount = useChatStore((s) => s.backgroundTaskCount);
  const tick = useWorkingLabelTick();
  const label = workingIndicatorLabel(bgCount, tick);
  return (
    <Message from="assistant" data-testid="working-indicator" aria-hidden="true">
      <MessageContent>
        <div className="flex items-center gap-1.5 py-0.5">
          <OttoIcon className="otto-working h-4 w-auto shrink-0" />
          <Shimmer className="text-xs font-mono" duration={1.5}>
            {label}
          </Shimmer>
        </div>
      </MessageContent>
    </Message>
  );
}

/**
 * Decide whether to render the main chat's "Working…" indicator.
 *
 * Lit for the whole busy turn — through streaming text, tool runs, and
 * reasoning gaps — so the user always sees the session is still going. A
 * reload can hydrate a custom-agent session as ``running`` before any bubble
 * exists locally; the indicator stays visible in that empty-but-busy state
 * too.
 *
 * @param showsWorking - True when the session snapshot or local response
 *   state says the main session is still working.
 * @param bubbles - Rendered chat bubbles currently hydrated in the main
 *   session, e.g. assistant, user, or compaction-loading bubbles.
 * @returns True when the standalone working indicator should render; false
 *   when the session is idle, or a compaction-loading bubble is last and
 *   already represents the busy state with its own animation.
 */
export function shouldShowWorkingIndicator(showsWorking: boolean, bubbles: Bubble[]): boolean {
  if (!showsWorking) return false;
  return bubbles[bubbles.length - 1]?.kind !== "compaction_loading";
}

/**
 * Band copy for each in-flight managed-sandbox launch stage, in
 * pipeline order: provisioning → cloning (repo workspaces only) →
 * starting → connecting. `starting` is the in-sandbox host booting
 * and dialing back to the server (so it reads "Connecting host");
 * `connecting` is the agent runner being launched on that host
 * (so it reads "Starting agent"). Terminal stages are absent on
 * purpose — `ready` clears the band and `failed` renders its own
 * error band.
 */
const SANDBOX_STAGE_LABELS: Record<string, string | undefined> = {
  provisioning: "Provisioning sandbox",
  cloning: "Cloning repository",
  starting: "Connecting host",
  connecting: "Starting agent",
};

/**
 * Failure band for a managed-sandbox session whose background launch
 * died. Renders the recorded reason so a dead launch explains itself
 * instead of presenting a silent dead chat. In-flight launch progress
 * does NOT render here — it shares the in-thread
 * :func:`RunnerStartingIndicator` spot so all launch states live on
 * one consistent line.
 */
export function SandboxFailedIndicator({ status }: { status: SandboxStatus }) {
  return (
    <div
      data-testid="sandbox-failed-indicator"
      role="status"
      className={cn(
        "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-destructive text-xs",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <AlertTriangleIcon className="size-3.5 shrink-0" aria-hidden />
      <span>Sandbox launch failed{status.error ? `: ${status.error}` : ""}</span>
    </div>
  );
}

export function ConnectionIndicator({
  liveness,
  onShowReconnectHelp,
  surfaceFrontmost = true,
}: {
  liveness: SessionLiveness;
  onShowReconnectHelp: () => void;
  // Whether the chat/terminal surface is frontmost (not under a drawer). Gates
  // the native iOS bar so it doesn't float over an opened sidebar/panel.
  surfaceFrontmost?: boolean;
}) {
  const terminalFirst = useTerminalFirst();
  const keyboardVisible = useIOSNativeKeyboardVisible(
    terminalFirst?.isTerminalFirst === true,
    terminalFirst?.view === "chat",
  );
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  // Genuinely-unreachable states get the reconnect banner, for
  // both terminal-first and regular sessions. `runner_asleep` (host up,
  // runner relaunches on the next message), `host_asleep` (resumable managed
  // host the server wakes on the next message), and `unknown` (pre-poll) are
  // NOT unreachable — they're handled below.
  const unreachable = liveness.kind === "host_offline" || liveness.kind === "local_stranded";

  // In the iOS shell the Chat/Terminal toggle is the native Liquid Glass bar,
  // not the in-page pill. Drive it from here (always mounted) with the SAME
  // visibility the pill would have, expressed as a stable boolean so switching
  // views never flickers the bar. Hook is called unconditionally (before any
  // early return) to satisfy the rules of hooks.
  const nativeBarVisible =
    isIOSShell() &&
    terminalFirst?.isTerminalFirst === true &&
    !terminalFirst.isShellView &&
    sandboxStatus?.stage !== "failed" &&
    !unreachable &&
    !keyboardVisible &&
    surfaceFrontmost;
  useNativeChatTerminalBar(terminalFirst, nativeBarVisible);

  if (sandboxStatus !== null) {
    // A failed launch owns this band with its reason. An IN-FLIGHT
    // launch renders in the chat thread (RunnerStartingIndicator)
    // instead — but still suppresses the liveness bands below, which
    // would misread the not-yet-bound session as stranded.
    if (sandboxStatus.stage === "failed") {
      return <SandboxFailedIndicator status={sandboxStatus} />;
    }
    return null;
  }
  if (unreachable) {
    return (
      <button
        type="button"
        data-testid="disconnected-indicator"
        onClick={onShowReconnectHelp}
        className={cn(
          "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-xs text-destructive underline-offset-2 hover:underline",
          CHAT_COLUMN_WIDTH,
        )}
      >
        <WifiOffIcon className="size-3.5 shrink-0" />
        <span>
          {liveness.kind === "host_offline"
            ? "Host is offline — click to reconnect"
            : "Agent disconnected — click to reconnect"}
        </span>
      </button>
    );
  }

  // Terminal-first sessions own the Chat/Terminal toggle for EVERY
  // reachable state — `online`, `unknown` (pre-poll), `starting`
  // (spinning up / relaunching), AND `runner_asleep` (stopped, host
  // alive). Only the unreachable states above replace it with the banner.
  // Keeping the pill visible through `runner_asleep` is why stopping a
  // runner no longer makes the toggle vanish: the pill stays, and the
  // next send (or a fresh launch) drives its own terminal-pending spinner
  // as the runner comes back. The strict `runner_online` still gates the
  // inline PTY *view* (it needs a live tunnel) — but not the toggle.
  if (terminalFirst?.isTerminalFirst) {
    // In the iOS shell the toggle is the native bar (driven above). Render only
    // a spacer reserving its fixed footprint so the composer clears it — and
    // nothing when the bar is hidden.
    if (isIOSShell()) {
      // Chat reserves a touch less than terminal: the composer's own bottom
      // content (the status line) already cushions the gap to the bar.
      return nativeBarVisible ? (
        <div
          aria-hidden
          className={cn(
            "omnigent-native-bottom-spacer",
            terminalFirst.view === "chat" && "omnigent-native-bottom-spacer--chat",
          )}
        />
      ) : null;
    }
    // A rail-opened shell owns the main view chrome-free — no pill: a
    // "Chat" option under someone else's shell misreads as the shell
    // being the agent. The shell view carries its own close affordance
    // (MainTerminalView's X) back to chat.
    if (terminalFirst.isShellView) return null;
    if (keyboardVisible) return null;
    return <ConnectedTerminalFirstPill ctx={terminalFirst} />;
  }

  // A regular (non-terminal-first) session whose runner is still spinning
  // up shows a passive "Connecting…" row — no action, no banner, just a
  // heartbeat so the empty chat doesn't read as broken.
  if (liveness.kind === "starting") {
    return (
      <div
        data-testid="connecting-indicator"
        className={cn(
          "mx-auto mb-4 flex w-full items-center justify-center gap-2 px-6 py-1.5 text-muted-foreground text-xs",
          CHAT_COLUMN_WIDTH,
        )}
      >
        <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
        <span>Connecting…</span>
      </div>
    );
  }

  // `online`/`unknown` for a non-terminal-first session and
  // `runner_asleep`/`host_asleep` for any session: status lives in the
  // sidebar / the composer stays open, so render nothing here.
  return null;
}

/**
 * Main-pane launch indicator — the single in-thread line for every
 * "session is coming up" state. Two launch shapes feed it, in
 * priority order:
 *
 * 1. A managed-sandbox launch (`sandboxStatus` in flight): shows the
 *    current pipeline stage ("Provisioning sandbox…", "Cloning
 *    repository…", …) for ANY session type.
 * 2. A terminal-first runner spin-up (`terminalStartingUp`): shows the
 *    generic "Starting up…" terminal copy. The sandbox stages win
 *    while both are active — they're strictly more specific.
 *
 * Self-gates to null when neither applies. `hero` is the centered
 * empty-state placeholder (no bubbles yet); `row` is the in-thread
 * spinner beneath the user's first message (the create-then-send path
 * renders that bubble immediately, so the empty state never shows
 * there).
 */
export function RunnerStartingIndicator({ variant }: { variant: "hero" | "row" }) {
  const terminalFirst = useTerminalFirst();
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  // `ready` never reaches the store (cleared) and `failed` renders the
  // destructive band in ConnectionIndicator — only in-flight stages
  // with known copy show here.
  const sandboxLabel =
    sandboxStatus !== null && sandboxStatus.stage !== "failed"
      ? SANDBOX_STAGE_LABELS[sandboxStatus.stage]
      : undefined;
  // `terminalStartingUp` is computed for ALL sessions in AppShell (it does not
  // check isTerminalFirst), so gate on isTerminalFirst too: regular agents
  // (e.g. polly) get the generic ConnectionIndicator "Connecting…" band and
  // must not also render this.
  const terminalSpinUp = Boolean(
    terminalFirst?.isTerminalFirst && terminalFirst.terminalStartingUp,
  );
  if (sandboxLabel === undefined && !terminalSpinUp) {
    return null;
  }
  const line = sandboxLabel !== undefined ? `${sandboxLabel}…` : "Starting up…";
  // role=status + aria-live so assistive tech announces the transient wait;
  // the spinner glyph itself is decorative (aria-hidden).
  if (variant === "hero") {
    return (
      <ConversationEmptyState
        data-testid="runner-starting-indicator"
        role="status"
        aria-live="polite"
        icon={<Loader2Icon className="size-7 animate-spin" aria-hidden />}
        title={sandboxLabel !== undefined ? `${sandboxLabel}…` : "Starting up…"}
        description={
          sandboxLabel !== undefined
            ? "Setting up your sandbox — this can take a minute."
            : "This can take a few seconds."
        }
      />
    );
  }
  return (
    <Message
      from="assistant"
      data-testid="runner-starting-indicator"
      role="status"
      aria-live="polite"
    >
      <MessageContent>
        <span className="flex items-center gap-2 text-muted-foreground text-sm">
          <Loader2Icon className="size-4 shrink-0 animate-spin" aria-hidden />
          {line}
        </span>
      </MessageContent>
    </Message>
  );
}

/**
 * Mirrors the Chat/Terminal state onto the iOS shell's native Liquid Glass
 * switcher and routes its taps back into `setView`. Driven by a stable
 * `visible` boolean (not this hook's mount/unmount), so toggling Chat/Terminal
 * updates the bar in place instead of flickering it hidden→shown. A no-op
 * outside the iOS shell; the caller renders its own in-page pill there.
 */
function useNativeChatTerminalBar(
  ctx: ReturnType<typeof useTerminalFirst> | null,
  visible: boolean,
): void {
  const native = isIOSShell();
  const view = ctx?.view ?? "chat";
  const terminalsAvailable = ctx?.terminalsAvailable ?? false;
  const terminalStartingUp = ctx?.terminalStartingUp ?? false;

  // Keep `setView` reachable from the subscribe-once effect without
  // resubscribing whenever the callback identity changes.
  const setViewRef = useRef(ctx?.setView);
  setViewRef.current = ctx?.setView;

  // Push current state + visibility down whenever any of it changes.
  useEffect(() => {
    if (!native) return;
    setNativeViewMode({
      mode: view,
      terminalEnabled: terminalsAvailable,
      terminalStartingUp,
      visible,
    });
  }, [native, view, terminalsAvailable, terminalStartingUp, visible]);

  // Belt-and-suspenders: hide the bar if the host component ever unmounts.
  useEffect(() => {
    if (!native) return;
    return () => {
      setNativeViewMode({
        mode: "chat",
        terminalEnabled: false,
        terminalStartingUp: false,
        visible: false,
      });
    };
  }, [native]);

  // Route native taps back into the web layer.
  useEffect(() => {
    if (!native) return;
    return onNativeViewModeChanged((mode) => setViewRef.current?.(mode));
  }, [native]);
}

/**
 * Chat/Terminal segmented control for terminal-first sessions. Status
 * lives in the sidebar — this band is purely a view toggle.
 *
 * Only rendered outside the iOS shell; inside it the switcher is drawn natively
 * (Liquid Glass) over the web view — see {@link useNativeChatTerminalBar}.
 */
function ConnectedTerminalFirstPill({
  ctx,
}: {
  ctx: NonNullable<ReturnType<typeof useTerminalFirst>>;
}) {
  // `terminalStartingUp` is the single loading signal — AppShell folds the
  // launch (liveness `starting`) and PTY-creation (`terminalPending`)
  // sources into it. The button is disabled whenever no terminal is
  // reachable: greyed-and-spinning reads as "loading", greyed-and-static as
  // "no terminal / stopped".
  const { view, setView, terminalsAvailable, terminalStartingUp } = ctx;

  return (
    <div
      className={cn(
        "terminal-first-switcher-container mx-auto flex w-full items-center justify-center px-6 pb-1.5",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <div
        role="group"
        aria-label="View mode"
        className="terminal-first-switcher flex items-center gap-1 rounded-full border border-border bg-card/90 p-1 text-xs shadow-sm"
      >
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            aria-pressed={view === "chat"}
            aria-label="Chat"
            onClick={() => setView("chat")}
            className={cn(
              "terminal-first-switcher-option flex cursor-pointer items-center gap-1 rounded-full px-2 py-0.5 transition-colors",
              view === "chat"
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            )}
          >
            <MessageSquareIcon className="size-3.5 shrink-0" />
            <span>Chat</span>
          </button>
          <button
            type="button"
            aria-pressed={view === "terminal"}
            aria-label="Terminal"
            disabled={!terminalsAvailable}
            title={terminalStartingUp ? "Terminal is starting up…" : undefined}
            onClick={() => setView("terminal")}
            className={cn(
              "terminal-first-switcher-option flex cursor-pointer items-center gap-1 rounded-full px-2 py-0.5 transition-colors disabled:cursor-not-allowed disabled:opacity-50",
              view === "terminal"
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            )}
          >
            {terminalStartingUp ? (
              <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
            ) : (
              <TerminalIcon className="size-3.5 shrink-0" />
            )}
            <span>Terminal</span>
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Whether a user-role bubble is a runtime-injected `[System: ...]`
 * notification (rendered via SystemMessageView, not as a normal user
 * bubble). Matches the gate in `UserBubble`: pure text, no attachments,
 * recognizable system header.
 */
function isSystemBubble(bubble: Bubble): boolean {
  if (bubble.kind !== "user") return false;
  const hasAttachments = bubble.content.some(
    (c) => c.type === "input_image" || c.type === "input_file",
  );
  if (hasAttachments) return false;
  return parseSystemMessage(extractUserText(bubble.content)) !== null;
}

function CompactionLoadingIndicator() {
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef(performance.now());

  useEffect(() => {
    const id = window.setInterval(() => {
      setElapsed(Math.round((performance.now() - startRef.current) / 1000));
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <Message from="assistant" data-testid="compacting-indicator">
      <MessageContent>
        <div className="flex items-center gap-2 text-xs font-mono">
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

// Memoized so a streaming delta (which rebuilds the whole bubble array) only
// re-renders the bubble that actually changed, not every prior message's
// markdown/syntax-highlighting subtree. See `bubblesEqual`. Exported for
// the user-bubble markdown render tests.
export const BubbleView = memo(
  function BubbleView({ bubble }: { bubble: Bubble }) {
    if (bubble.kind === "user") return <UserBubble bubble={bubble} />;
    if (bubble.kind === "compaction_loading") {
      return <CompactionLoadingIndicator />;
    }
    if (bubble.kind === "compaction") return <CompactionMarker />;
    if (bubble.kind === "routing_decision") {
      return (
        <RoutingDecisionChip
          model={bubble.model}
          tier={bubble.tier}
          applied={bubble.applied}
          rationale={bubble.rationale}
        />
      );
    }
    return <AssistantBubble bubble={bubble} />;
  },
  (prev, next) => bubblesEqual(prev.bubble, next.bubble),
);

function UserBubble({ bubble }: { bubble: Extract<Bubble, { kind: "user" }> }) {
  const sessionId = useChatStore((s) => s.conversationId);
  // Author labels only matter once the session is shared with someone else.
  const isSessionShared = useContext(SessionSharedContext);
  // Plain-text path is the common case.
  // - input_image: render inline <img> when the file is uploaded (file_id
  //   doesn't start with "pending:"); show a chip while the upload is
  //   in-flight.
  // - input_file: always render as a chip (non-image files can't be
  //   previewed inline).
  const text = extractUserText(bubble.content);
  const images = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_image" }> => c.type === "input_image",
  );
  const fileChips = bubble.content.filter(
    (c): c is Extract<MessageContentBlock, { type: "input_file" }> => c.type === "input_file",
  );
  // "@"-mentioned workspace files/folders ride in as "[Attached: …]" text
  // markers (no input_file block), so surface them as chips — otherwise the
  // marker is stripped and the user can't see what they attached.
  const mentionedChips = extractAttachedPaths(bubble.content);
  // Runtime-injected `[System: ...]` notifications (task completion,
  // timer firings, terminal idle) ride in on role=user. When the content
  // is a pure system marker — no attached images or files — swap the
  // normal bubble for a muted centered indicator.
  if (images.length === 0 && fileChips.length === 0 && mentionedChips.length === 0) {
    const parsed = parseSystemMessage(text);
    if (parsed) return <SystemMessageView message={parsed} />;
  }
  // Badge OTHER contributors' messages only (never your own) — an avatar
  // circle + author-tinted bubble, not an email label.
  const author = bubble.createdBy;
  const showAuthorBadge = shouldShowAuthorBadge(author, getCurrentAuthorId(), isSessionShared);
  // Equality selector so Zustand only re-renders the matching bubble.
  const flashing = useChatStore((s) => s.flashItemId === bubble.itemId);
  const [isCopied, setIsCopied] = useState(false);
  const copyTimeoutRef = useRef<number>(0);

  const handleCopy = async () => {
    if (!text || !navigator?.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(text);
      setIsCopied(true);
      window.clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
    } catch {
      // ignore clipboard errors
    }
  };

  return (
    <Message
      from="user"
      data-testid="message-bubble"
      data-role="user"
      data-user-message-id={bubble.itemId}
      className="max-w-3xl"
    >
      {/* w-fit + ml-auto shrink-wrap the row so the author avatar sits
          immediately left of the right-aligned bubble (the bubble's own
          ml-auto has no free space to absorb inside a fit-width row). */}
      <div className="ml-auto flex w-fit max-w-full items-center gap-1.5">
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
          // alpha instead of the default bg-muted, so authorship reads at
          // a glance without any email text.
          style={showAuthorBadge && author ? { backgroundColor: userColorTint(author) } : undefined}
        >
          {/* Inline image previews */}
          {images.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-2">
              {images.map((img, i) =>
                img.file_id.startsWith("pending:") ? (
                  // Upload in-flight — show a chip placeholder
                  <span
                    key={i}
                    className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
                  >
                    <ImageIcon className="size-3 shrink-0" />
                    <span className="max-w-[180px] truncate">
                      {img.filename ?? img.file_id.replace("pending:", "")}
                    </span>
                  </span>
                ) : (
                  // Uploaded — render the actual image
                  <SessionImage
                    key={i}
                    path={
                      sessionId
                        ? `/v1/sessions/${encodeURIComponent(sessionId)}/resources/files/${encodeURIComponent(img.file_id)}/content`
                        : undefined
                    }
                    alt={img.filename ?? img.file_id}
                    className="max-h-64 max-w-full rounded-md object-contain"
                  />
                ),
              )}
            </div>
          )}
          {/* Non-image file chips */}
          {fileChips.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1.5">
              {fileChips.map((att, i) => (
                <span
                  key={i}
                  className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
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
                  className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
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
          {/* Render user text as markdown, matching the assistant bubble
            (headings, lists, code fences, file-path links). `breaks` keeps
            single newlines as line breaks — users type multi-line messages
            without blank-line paragraph separators and expect their line
            breaks preserved. Empty text — e.g. an attachments-only message —
            renders nothing rather than an empty markdown block. */}
          {text && <FilePathAwareMessageResponse breaks>{text}</FilePathAwareMessageResponse>}
        </MessageContent>
      </div>
      {text && (
        <MessageActions className="mt-1 ml-auto opacity-40 transition-opacity md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
          <MessageAction tooltip="Copy" onClick={handleCopy}>
            {isCopied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
          </MessageAction>
        </MessageActions>
      )}
    </Message>
  );
}

function AssistantBubble({ bubble }: { bubble: Extract<Bubble, { kind: "assistant" }> }) {
  // The walker only emits an assistant bubble when at least one
  // assistant-side block exists, so `items` is non-empty here in the
  // common case. The "Working…" shimmer for the empty-items / streaming
  // gap is rendered at the page level, not inside this component.
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const [isCopied, setIsCopied] = useState(false);
  const copyTimeoutRef = useRef<number>(0);
  // null outside AppShell's provider (isolated tests) → hide the action.
  const forkDialog = useForkDialog();

  if (bubble.items.length === 0) return null;

  const markdownText = collectBubbleMarkdown(bubble.items);

  // Elicitation cards (e.g. AskUserQuestion form) want full chat-column
  // width to match the composer, not the default w-fit shrink-to-content.
  const hasElicitation = bubble.items.some((it) => it.kind === "elicitation");
  const isWide = hasElicitation || containsMarkdownTable(bubble.items);

  const handleCopy = async () => {
    if (!markdownText || !navigator?.clipboard?.writeText) return;
    try {
      await navigator.clipboard.writeText(markdownText);
      setIsCopied(true);
      window.clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
    } catch {
      // ignore clipboard errors
    }
  };

  return (
    <>
      <Message
        from="assistant"
        data-testid="message-bubble"
        data-role="assistant"
        className={isWide ? "max-w-full" : "max-w-3xl"}
      >
        <MessageContent className={isWide ? "w-full" : undefined}>
          <BlockRenderer items={bubble.items} sessionStatus={sessionStatus} />
        </MessageContent>
        {bubble.lifecycle === "cancelled" && (
          <p
            className="mt-1 flex items-center gap-1 text-xs text-muted-foreground"
            data-testid="assistant-interrupted-indicator"
          >
            <XIcon className="size-3" aria-hidden="true" />
            <span>Interrupted</span>
          </p>
        )}
        {markdownText && (
          <MessageActions className="mt-1 opacity-40 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
            <MessageAction tooltip="Copy" onClick={handleCopy}>
              {isCopied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
            </MessageAction>
            {/* Fork from this response: clone the session with history
                truncated after this turn. Hidden while the response is
                still streaming (its items aren't committed yet) and when
                the session can't be forked (sub-agent / isolated mount). */}
            {forkDialog?.canFork && bubble.lifecycle !== "streaming" && (
              <MessageAction
                tooltip="Fork from here"
                data-testid="fork-from-response"
                onClick={() => forkDialog.openForkDialog({ upToResponseId: bubble.responseId })}
              >
                <GitForkIcon size={14} />
              </MessageAction>
            )}
          </MessageActions>
        )}
      </Message>

      {bubble.lifecycle === "failed" && (
        <p className="text-destructive text-xs">Error: {bubble.error}</p>
      )}
    </>
  );
}

interface ComposerProps {
  status: "idle" | "streaming";
  /** Local stream OR cross-client `session.status: running`. */
  isWorking: boolean;
  disabled: boolean;
  onSend: (text: string, files?: File[]) => void;
  /**
   * Send a recognised skill as a `slash_command` event (the REPL's wire
   * shape) instead of plaintext. When present and the typed command names
   * a known session skill, `submit()` routes through this; otherwise the
   * command falls through to `onSend` as plaintext. Undefined for
   * native-terminal sessions, which always send `/skill` as plaintext so
   * the vendor TUI loads the skill itself.
   */
  onSendSlashCommand?: (name: string, args: string) => void;
  onStop: () => void;
  agents: Agent[] | undefined;
  agentsLoading: boolean;
  selectedAgentId: string | null;
  onSelectAgent: (id: string) => void;
  permissionLevel: number | null;
  /**
   * When non-null, the composer is forced read-only and the string is
   * shown as the textarea placeholder. Distinct from
   * ``permissionLevel === 1`` (which means "user has read-only
   * grant") — this captures the "this session structurally can't be
   * interacted with" case: e.g. a claude-native sub-agent whose
   * transcript is mirrored from disk and has no input surface. ``null``
   * leaves the existing ``permissionLevel`` gate alone.
   */
  readOnlyReason: string | null;
  /** Quoted texts to prepend to the next message (one per "Reply ↵" click). */
  replyQuotes: string[];
  /** Removes the quote at the given index without submitting. */
  onRemoveQuote: (index: number) => void;
  /** Clears all quotes (called after submit). */
  onClearAllQuotes: () => void;
  /** Reasoning-effort options to render in `/effort` and the picker dropdown. */
  effortLevels: readonly string[];
  /** Show `/effort` and the Effort picker section. */
  showEffort: boolean;
  /** Whether the picker dropdown should include a Models section. */
  showModels: boolean;
  /** Native model picker family, when present. */
  modelPickerKind: NativeModelPickerKind | null;
  /** Codex app-server model options for codex-native sessions. */
  codexModelOptions: readonly CodexModelOption[];
  /** Show the Codex Plan-mode toggle. */
  showCodexPlanMode: boolean;
  /** Show the Codex Goal control. */
  showCodexGoal?: boolean;
  /**
   * Terminal-first session (Chat/Terminal pill present). Presentation
   * only: tightens the composer's bottom padding to `pb-1.5` so it sits
   * closer to the pill beneath it; non-terminal-first chats use the
   * roomier `pb-3`.
   */
  isTerminalFirst?: boolean;
  /**
   * Native-CLI wrapper session (claude-native / codex-native). Drops the
   * `/model` slash command unless the session also has a model picker
   * (`showModels`); terminal-first SDK sessions (embedded Omnigent REPL
   * terminal) keep it.
   */
  isNativeWrapper?: boolean;
  /**
   * The session's runner is asleep but its host is online (`runner_asleep`):
   * the composer stays enabled and the placeholder nudges the user to send a
   * message, which relaunches the runner on the live host. Ignored while a
   * turn is streaming (the follow-up placeholder wins).
   */
  reconnectHint?: boolean;
  /**
   * The session is host-bound to a dormant resumable managed host that is
   * offline (`host_asleep`): the composer stays enabled, and the placeholder
   * tells the user their next message will resume the sandbox host (which can
   * take a few minutes) so the wake latency is expected, not surprising.
   * Ignored once a turn is streaming.
   */
  sandboxAsleepHint?: boolean;
  /**
   * The session is unreachable (`host_offline` / `local_stranded`): a message
   * can't wake it. The composer is blocked (disabled) and the reconnect
   * banner below is the only affordance.
   */
  unreachable?: boolean;
  /** Latest parsed advisor verdict for the cost-routing pill; `null`/omitted when none. */
  costRoutingVerdict?: CostRoutingVerdict | null;
  /** Session passes `isCostRoutingSession` (polly orchestrator, not a child); see that predicate. */
  costRoutingEligible?: boolean;
  /**
   * Sub-agent instance label when the active session is a child, e.g.
   * ``"check-account-eligibility"``; ``null``/omitted for top-level
   * sessions. When set, the composer peeks a "Chatting with sub-agent …"
   * tray above the card. See ``subAgentComposerLabel``.
   */
  subAgentLabel?: string | null;
}

/**
 * Build the full slash-command map for the composer: built-ins
 * first (so they top the menu), then one entry per session skill
 * keyed by ``/${skill.name}``. Insertion order matters — the
 * menu iterates ``Object.entries`` and the user sees built-ins
 * before skills.
 *
 * :param skills: ``Session.skills`` from the snapshot, defaulting
 *     to ``[]`` when the wire field is absent (older servers).
 * :param showEffort: Whether this session supports Web UI effort controls.
 * :param showModel: Whether to include ``/model`` (in-process sessions
 *     and claude-native, which both honor ``conv.model_override``; see
 *     the call site).
 * :returns: Merged ``Record<command, description>``.
 */
export function buildSlashCommandMap(
  skills: ReadonlyArray<{ name: string; description: string }>,
  showEffort: boolean,
  showModel: boolean,
  showCompact: boolean = true,
): Record<string, string> {
  const m: Record<string, string> = {};
  for (const [name, description] of Object.entries(BUILTIN_SLASH_COMMANDS)) {
    if (name === "/effort" && !showEffort) continue;
    if (name === "/model" && !showModel) continue;
    if (name === "/compact" && !showCompact) continue;
    m[name] = description;
  }
  for (const skill of skills) {
    m[`/${skill.name}`] = skill.description;
  }
  return m;
}

/**
 * Set of slash commands that should fill the textarea with
 * ``"/cmd "`` on menu selection rather than executing immediately.
 * Includes the arg-taking built-ins (each gated on its own capability
 * flag) plus every session skill — skills never auto-execute on
 * selection; the user sends them, and :func:`Composer.submit` routes a
 * known skill to a ``slash_command`` event (in-process) or plaintext
 * (native sessions).
 *
 * :param skills: ``Session.skills`` from the snapshot.
 * :param showEffort: Whether ``/effort`` should be selectable.
 * :param showModel: Whether ``/model`` should be selectable (same gate
 *     as :func:`buildSlashCommandMap`'s ``showModel``).
 * :returns: A ``Set`` of slash-prefixed names.
 */
export function buildSlashCommandWithArgsSet(
  skills: ReadonlyArray<{ name: string; description: string }>,
  showEffort: boolean,
  showModel: boolean,
): Set<string> {
  const s = new Set<string>();
  if (showEffort) s.add("/effort");
  if (showModel) s.add("/model");
  for (const skill of skills) s.add(`/${skill.name}`);
  return s;
}

/** Circumference of the progress ring (r=5.5). */
const RING_CIRCUMFERENCE = 2 * Math.PI * 5.5;

/** Circular progress ring showing how much context window is used, with the used percentage beside it. */
function ContextRing({ contextWindow, tokensUsed }: { contextWindow: number; tokensUsed: number }) {
  const pct = Math.min(tokensUsed / contextWindow, 1);
  // Arc, %, label, and tooltip all encode context USED: a fresh session
  // shows an empty ring at 0% and the ring fills as context is consumed.
  const usedArc = pct * RING_CIRCUMFERENCE;
  const usedPct = Math.round(pct * 100);

  const color =
    pct > 0.8 ? "text-destructive" : pct > 0.6 ? "text-warning" : "text-muted-foreground";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn("flex items-center gap-1.5", color)}
          aria-label={`${usedPct}% of context used`}
        >
          <svg viewBox="0 0 16 16" width="16" height="16" fill="none" aria-hidden="true">
            {/* Track */}
            <circle cx="8" cy="8" r="5.5" stroke="currentColor" strokeWidth="2" opacity="0.2" />
            {/* Used arc — skipped at 0, where round linecaps would still paint a dot. */}
            {usedArc > 0 && (
              <circle
                cx="8"
                cy="8"
                r="5.5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeDasharray={`${usedArc} ${RING_CIRCUMFERENCE}`}
                transform="rotate(-90 8 8)"
              />
            )}
          </svg>
          <span className="text-xs tabular-nums" aria-hidden="true">
            {usedPct}%
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-44 text-center text-xs">
        <p className="tabular-nums">{usedPct}% of context used.</p>
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Model label for the composer status tray.
 *
 * @param model - Model override or bound agent model id.
 * @param codexModelOptions - Codex-returned model metadata, when available.
 * @returns Codex's display label for known Codex models, a local Claude alias
 *   label for Claude native tiers, the raw model id otherwise, or ``null``
 *   when no model is known.
 */
export function formatStatusModelLabel(
  model: string | null,
  codexModelOptions: readonly CodexModelOption[] = [],
): string | null {
  const raw = model?.trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const codexOption = findCodexModelOption(codexModelOptions, raw);
  if (codexOption) return codexOption.displayName ?? codexOption.id;
  const known = CLAUDE_NATIVE_MODELS.find((m) => m.id === lower);
  if (known) return known.label;
  return raw;
}

function formatStatusEffortLabel(effort: string | null, raw = false): string | null {
  if (!effort) return null;
  if (raw) return effort;
  return effort.toLowerCase() === "xhigh" ? "xHigh" : formatEffortLabel(effort);
}

/**
 * Compose the current model and effort for the composer status tray.
 *
 * @param model - Model override or bound model id.
 * @param effort - Current reasoning effort override, if any.
 * @returns Compact label such as ``"gpt-5.5 xhigh"``.
 */
export function formatModelEffortStatusLabel(
  model: string | null,
  effort: string | null,
  codexModelOptions: readonly CodexModelOption[] = [],
): string | null {
  const codexOption = model ? findCodexModelOption(codexModelOptions, model.trim()) : null;
  const modelLabel = formatStatusModelLabel(model, codexModelOptions);
  const effortLabel = formatStatusEffortLabel(effort, codexOption !== null);
  const parts = [modelLabel, effortLabel].filter((p): p is string => p != null && p.length > 0);
  return parts.length > 0 ? parts.join(" ") : null;
}

/**
 * Identity label for the composer status tray: which harness/agent is
 * running this session. Native vendor wrappers read as the bare vendor
 * name ("Claude" / "Codex"); SDK/bundle agents read as the agent name
 * with the brain harness in parens ("Polly (Pi)"). This moved OUT of the
 * picker trigger (which now shows model/effort) — the trigger is the
 * model/effort control, so the harness identity belongs in the read-only
 * shelf below.
 *
 * @param modelPickerKind - Native picker family, when the session is a
 *   claude-/codex-/cursor-native wrapper.
 * @param agentName - Bound agent name (lowercase slug), if any.
 * @param sessionHarness - Effective brain harness id (override-aware).
 * @returns Display label, or ``null`` when nothing is known.
 */
export function composerHarnessLabel(
  modelPickerKind: NativeModelPickerKind | null,
  agentName: string | null | undefined,
  sessionHarness: string | null,
  harnessLabels: Record<string, string> = BRAIN_HARNESS_LABELS,
): string | null {
  if (modelPickerKind === "claude") return "Claude";
  if (modelPickerKind === "codex") return "Codex";
  if (modelPickerKind === "cursor") return "Cursor";
  if (modelPickerKind === "kiro") return "Kiro";
  if (modelPickerKind === "opencode") return "OpenCode";
  const display = agentName ? agentDisplayLabel(agentName) : null;
  const harness = sessionHarness ? (harnessLabels[sessionHarness] ?? null) : null;
  if (display && harness) return `${display} (${harness})`;
  return display ?? harness;
}

/**
 * Status-line tray tucked behind the composer card: the worktree branch
 * on the left (truncated to an ellipsis so the tray never wraps), the
 * model/effort + context ring on the right. Shares the card's background so the two
 * read as one rounded shape: the card keeps its full rounded-2xl and
 * paints on top (it's position:relative), while this in-flow sibling is
 * pulled up behind it so a rounded shelf peeks out below the card's
 * bottom edge — the card's own bottom border is the divider. Owns the
 * visibility guards so an empty tray never renders — no dead shelf when
 * the session has nothing to report. Session cost lives in the header
 * agent-info popover (the "i" button), not here.
 */
function ComposerStatusLine({
  harnessLabel,
  codexGoal,
  isSubAgentSession,
}: {
  harnessLabel: string | null;
  codexGoal: CodexGoal | null;
  isSubAgentSession: boolean;
}) {
  const conversationId = useChatStore((s) => s.conversationId);
  const contextWindow = useChatStore((s) => s.contextWindow);
  const tokensUsed = useChatStore((s) => s.tokensUsed);
  const codexPlanMode = useChatStore((s) => s.codexPlanMode);
  // Seeded from the session snapshot on bind (chatStore.sessionBindingPatch),
  // alongside contextWindow — so the branch reads from the same store as
  // the other status-line values rather than a separate fetch.
  const gitBranch = useChatStore((s) => s.gitBranch);

  const showBranch = !!conversationId && !!gitBranch;
  // Host indicator (green/red dot + host name), left of the worktree branch.
  // Hidden on sub-agent sessions — the header's child-session slot owns the
  // back affordance there, mirroring where this badge used to live. HostBadge
  // self-hides when the session isn't host-bound, so this is a visibility gate,
  // not a host-presence claim.
  const showHost = !!conversationId && !isSubAgentSession;
  // The harness/agent identity (e.g. "Claude", "Polly (Pi)") lives here now;
  // the picker trigger above owns the model/effort label since it's the
  // control that changes them.
  const showHarness = !!conversationId && harnessLabel !== null;
  const showPlanMode = !!conversationId && codexPlanMode;
  const showGoal = !!conversationId && codexGoal != null;
  // contextWindow > 0: the SSE path validates it but the snapshot path doesn't, and 0/0 → "NaN%".
  const showRing =
    !!conversationId && contextWindow != null && contextWindow > 0 && tokensUsed != null;
  if (!showBranch && !showPlanMode && !showGoal && !showRing && !showHarness) return null;

  return (
    <div
      data-testid="composer-status-line"
      className={cn(
        // -mt-4 slides the tray's square top corners up behind the card
        // (the 16px overlap exceeds the card's ~14px corner radius, so
        // they hide behind its straight sides); pt-5.5 (= --spacing *
        // 5.5) re-reserves the hidden region so the content sits below
        // the card's edge. bg-tray/40 (not bg-card) keeps it out of the
        // dark-mode glass rule — bg-card here would re-decorate the tray
        // with its own border/shadow, duplicating the composer's chrome —
        // and matches the home composer's footer tray surface.
        "mx-auto -mt-4 flex w-full items-center gap-3 rounded-b-2xl bg-tray/40 px-4 pb-1.5 pt-5.5",
        CHAT_COLUMN_WIDTH,
      )}
    >
      {/* Left: host badge then worktree branch. Always holds the flex-1 slot
          so the right cluster stays pinned right even when both are absent;
          each item truncates to an ellipsis so the tray never wraps. */}
      <div className="flex min-w-0 flex-1 items-center gap-3 text-xs text-muted-foreground">
        {showHost && conversationId && <HostBadge sessionId={conversationId} />}
        {showBranch && (
          <span className="flex min-w-0 items-center gap-1.5">
            <GitBranchIcon className="size-3.5 shrink-0" />
            <span data-testid="composer-git-branch" className="min-w-0 truncate" title={gitBranch}>
              {gitBranch}
            </span>
          </span>
        )}
      </div>
      {/* Right: model/effort and context ring, never shrinks. */}
      <div className="flex min-w-0 shrink-0 items-center gap-3">
        {showPlanMode && (
          <span
            data-testid="composer-plan-mode"
            className="inline-flex items-center gap-1 text-xs font-medium text-foreground"
          >
            <FileTextIcon className="size-3.5 shrink-0" />
            <span>Plan mode</span>
          </span>
        )}
        {showGoal && codexGoal && <CodexGoalStatusPill goal={codexGoal} />}
        {showHarness && harnessLabel && (
          <span
            data-testid="composer-harness"
            className="max-w-36 truncate text-xs text-muted-foreground sm:max-w-52"
            title={harnessLabel}
          >
            {harnessLabel}
          </span>
        )}
        {showRing && <ContextRing contextWindow={contextWindow} tokensUsed={tokensUsed} />}
      </div>
    </div>
  );
}

/**
 * Resolve the sub-agent instance label for the composer's "Chatting with
 * sub-agent …" tray, mirroring the Agents rail's child-row label
 * (``childPrimaryLabel`` in ``SubagentsPanel``).
 *
 * The spawn tool seeds a sub-agent's title as ``"{tool}:{name}"`` (e.g.
 * ``"claude_code:check-account-eligibility"``), so the human instance name
 * is the suffix after the first ``":"``. User-added rows carry a reserved
 * ``"ui:<agent>:<name>"`` sentinel; the ``"ui:"`` marker is stripped first
 * so the suffix is still the human name. Falls back to the bare title,
 * then the sub-agent type, then the bound agent name.
 *
 * @param session - The active session snapshot, or ``null`` while it loads
 *   / on the new-chat landing.
 * @returns The tray label, e.g. ``"check-account-eligibility"``; ``null``
 *   for a top-level session (no ``parentSessionId``) or when no snapshot is
 *   loaded — both hide the tray.
 */
export function subAgentComposerLabel(
  session: Pick<Session, "parentSessionId" | "title" | "subAgentName" | "agentName"> | null,
): string | null {
  if (!session || session.parentSessionId == null) return null;
  // Strip the user-added "ui:" sentinel so its "agent:name" suffix reads
  // like an LLM-spawned title.
  let title = session.title ?? null;
  if (title?.startsWith("ui:")) title = title.slice(3);
  if (title?.includes(":")) {
    const suffix = title.split(":").slice(1).join(":");
    if (suffix) return suffix;
  }
  // Last-resort display string: a sub-agent session always has a seeded
  // title in practice, so the final "sub-agent" only guards a degenerate
  // all-null snapshot (the tray still needs something to render).
  return title ?? session.subAgentName ?? session.agentName ?? "sub-agent";
}

/**
 * Peeking tray tucked behind the composer's top edge while the active
 * session is a sub-agent (child) — names the sub-agent the message is going
 * to, so the composer reads as "messaging the sub-agent", not the
 * orchestrator. Mirrors ``ComposerStatusLine`` (the worktree/context shelf
 * below the card) but rises above it: ``-mb-4`` slides the tray's square
 * bottom corners down behind the card (the 16px overlap exceeds the card's
 * ~14px corner radius, hiding them behind its straight sides) and ``pb-5.5``
 * re-reserves the hidden region so the label sits above the card's top edge.
 * The card is ``position:relative`` and paints on top, so its own top border
 * is the divider. Brand pink (``brand-accent``) marks this as a sub-agent
 * context cue, not a status.
 *
 * @param label - The sub-agent instance name, e.g.
 *   ``"check-account-eligibility"`` (from ``subAgentComposerLabel``).
 */
function SubagentComposerTray({ label }: { label: string }) {
  return (
    <div
      data-testid="composer-subagent-tray"
      className={cn(
        "mx-auto -mb-4 flex w-full items-center gap-1.5 rounded-t-2xl bg-brand-accent/10 px-4 pt-1.5 pb-5.5 text-xs text-brand-accent",
        CHAT_COLUMN_WIDTH,
      )}
    >
      <BotIcon className="size-3.5 shrink-0" aria-hidden="true" />
      {/* truncate so a long sub-agent name never wraps the tray to two rows */}
      <span className="min-w-0 truncate">
        Chatting with sub-agent <strong className="font-semibold">{label}</strong>
      </span>
    </div>
  );
}

/**
 * The message-input composer: textarea, attachments, slash-command
 * suggestions menu, and the send/stop controls. Exported for direct
 * unit testing of the slash-command keyboard behavior.
 */
export function Composer({
  status,
  isWorking,
  disabled,
  onSend,
  onSendSlashCommand,
  onStop,
  agents,
  agentsLoading,
  selectedAgentId,
  onSelectAgent,
  permissionLevel,
  readOnlyReason,
  replyQuotes,
  onRemoveQuote,
  onClearAllQuotes,
  effortLevels,
  showEffort,
  showModels,
  modelPickerKind,
  codexModelOptions,
  showCodexPlanMode,
  showCodexGoal = false,
  isTerminalFirst = false,
  isNativeWrapper = false,
  reconnectHint = false,
  sandboxAsleepHint = false,
  unreachable = false,
  costRoutingVerdict = null,
  costRoutingEligible = false,
  subAgentLabel = null,
}: ComposerProps) {
  const [value, setValue] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [planModeBusy, setPlanModeBusy] = useState(false);
  // Index of the highlighted item in the slash-command suggestions menu.
  // -1 means no item highlighted (menu closed or no matches). When the menu
  // opens with matches the reset logic below pre-selects the first item (0)
  // so Tab/Enter complete it immediately.
  const [menuIndex, setMenuIndex] = useState(-1);
  // Active "@"-file-mention being typed, plus its highlighted row and the
  // workspace paths the user has already tagged. ``@``-mention is wired for
  // the native coding-agent sessions (see ``mentionEnabled``): those harnesses
  // run in the workspace and read a file from an "[Attached: …]" marker, so a
  // tagged path is delivered by prepending that marker at send time — no
  // upload, the agent reads the on-disk file directly.
  // Active "@"-mention token (owned here; the shared useMentionBrowser hook
  // owns the selection index, tagged chips, and attach/drill/keyboard glue).
  const [mention, setMention] = useState<MentionState | null>(null);
  // Attachments pushed in from outside the composer (e.g. the file viewer's
  // "Attach to agent" button). Drained into ``mentionedItems`` below, then
  // cleared from the store so they aren't re-applied.
  const pendingComposerAttachments = useChatStore((s) => s.pendingComposerAttachments);
  // Nonce bumped when bare "/model" is submitted; opens the AgentPicker
  // dropdown instead of sending (see submit()).
  const [pickerOpenNonce, setPickerOpenNonce] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);
  // Highlight overlay mirroring the textarea; scroll-synced so the tinted
  // `/skill` token stays aligned once the draft grows past the visible rows.
  const backdropRef = useRef<HTMLDivElement>(null);
  const isStreaming = status === "streaming";

  // Read-only when either the user lacks a write grant OR the session
  // is structurally non-interactive (``readOnlyReason``). The
  // structural reason takes priority for the placeholder text since it
  // explains *why* this specific row can't receive input.
  const isReadOnly = permissionLevel === 1 || readOnlyReason !== null;
  // A pending elicitation addressed to this session parks the turn
  // server-side (the runner blocks on the verdict Future), so a message
  // sent now would sit queued and unread until the card is answered —
  // and for native wrappers the injected text could land in the vendor
  // TUI's permission prompt. Lock the composer until the verdict is in.
  // Mirrored sub-agent prompts (targetSessionId set to a child session)
  // don't gate this session's inbox, so they don't lock it.
  const hasPendingElicitation = useChatStore((s) =>
    s.blocks.some(
      (b) =>
        b.type === "elicitation" &&
        b.status === "pending" &&
        (b.targetSessionId == null || b.targetSessionId === s.conversationId),
    ),
  );

  // Per-session cost-control switch, hydrated from the snapshot on bind.
  const costControlModeOverride = useChatStore((s) => s.costControlModeOverride);
  const codexPlanMode = useChatStore((s) => s.codexPlanMode);
  // Harness/agent identity shown in the status tray below the card. The
  // picker trigger owns model/effort now, so the identity moves here.
  const sessionHarness = useChatStore((s) => s.sessionHarness);
  const subAgentName = useChatStore((s) => s.subAgentName);
  const brainHarnessLabels = useBrainHarnessLabels();
  const harnessLabel = composerHarnessLabel(
    modelPickerKind,
    // For a sub-agent (head) session, identify the head family being viewed
    // (e.g. the GPT head → "Gpt") rather than the bundle orchestrator
    // ("Debby") — the bundle is already named in the breadcrumb / Agents rail.
    subAgentName ??
      agents?.find((a) => a.id === selectedAgentId)?.name ??
      agents?.[0]?.name ??
      null,
    sessionHarness,
    brainHarnessLabels,
  );

  // Preserve unsent text + file attachments per session so switching
  // tabs and coming back restores the draft. The drafts map lives at
  // module scope (not useRef) because Composer unmounts during the
  // loading gate between session switches.
  const conversationId = useChatStore((s) => s.conversationId);
  const queuedMessages = useChatStore((s) => s.queuedMessages);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const flushBoundAgentId = useChatStore((s) => s.boundAgentId);
  const maybeFlushQueuedHead = useChatStore((s) => s.maybeFlushQueuedHead);
  const dequeueMessage = useChatStore((s) => s.dequeueMessage);
  const steerMessage = useChatStore((s) => s.steerMessage);
  const reorderQueuedMessage = useChatStore((s) => s.reorderQueuedMessage);
  // Drain the queue whenever idle with a waiting head — level-triggered so a
  // message queued right after the turn ended (or after an SSE reconnect that
  // carries no fresh idle transition) still sends instead of stranding. Hold
  // while unreachable: flushing would POST into a void (no executor / no host
  // to wake), bypassing onSend's reconnect dialog. The next reachable render
  // re-fires this effect and drains. `boundAgentId` is a dep because the flush
  // needs it: on navigate-back the binding lands after the status settles, and
  // without this dep the effect wouldn't re-fire to drain a queue for the
  // returned-to conversation.
  useEffect(() => {
    if (unreachable) return;
    maybeFlushQueuedHead();
  }, [
    status,
    sessionStatus,
    queuedMessages,
    conversationId,
    flushBoundAgentId,
    unreachable,
    maybeFlushQueuedHead,
  ]);
  const { goal: codexGoal, setGoal: setCodexGoal } = useCodexGoalState(
    conversationId,
    showCodexGoal,
  );
  // "@"-file-mention is scoped to the native coding-agent harnesses: their
  // vendor CLIs run in the workspace and read an on-disk file from an
  // attachment marker the executor already emits. In-process SDK sessions
  // get no mention menu, so the workspace listing is never fetched for them
  // (``enabled`` gate below). Codex's marker says "Attached file:" while the
  // others say "Attached:" — see ``mentionMarkerFor``. ``sessionHarness`` is
  // already read above for the status-tray harness label.
  // Derive from the canonical native-agent registry (which folds reversed
  // spellings like ``native-pi``) rather than a literal harness-string compare,
  // so the composer's "@" entry point can't split-brain from the file viewer's
  // "Attach to agent" gate (``canAttachToAgent``), which already uses it.
  const mentionEnabled = nativeCodingAgentForHarness(sessionHarness) !== undefined;
  const workspaceFilesQuery = useWorkspaceAllFiles(conversationId ?? undefined, {
    enabled: mentionEnabled,
  });
  const valueRef = useRef(value);
  valueRef.current = value;
  const filesRef = useRef(files);
  filesRef.current = files;
  // Guards against React StrictMode double-invoke in development:
  // setup → cleanup → setup runs cleanup before the user has touched
  // the input, which would delete the draft. Only save when the user
  // has actually changed the value since the last restore.
  const dirtyRef = useRef(false);
  // On mobile, programmatic focus immediately summons the software keyboard.
  // Keep desktop's fast-type affordance, but let mobile users explicitly tap
  // the composer when switching back from Terminal or changing sessions.
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches,
  );
  const isMobileRef = useRef(isMobile);
  isMobileRef.current = isMobile;
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  useEffect(() => {
    const restored = conversationId ? sessionDrafts.get(conversationId) : undefined;
    setValue(restored?.text ?? "");
    setFiles(restored?.files ?? []);
    dirtyRef.current = false;
    if (!isMobileRef.current) textareaRef.current?.focus();

    return () => {
      if (!conversationId || !dirtyRef.current) return;
      const text = valueRef.current;
      const draftFiles = filesRef.current;
      if (text || draftFiles.length > 0) {
        sessionDrafts.set(conversationId, { text, files: draftFiles });
      } else {
        sessionDrafts.delete(conversationId);
      }
      saveDraftsToStorage(sessionDrafts);
    };
  }, [conversationId]);

  // Adding a reply quote (via the floating "Reply" button) should drop the
  // caret straight into the composer so the user can type immediately. Only
  // focus when the count grows — removing a quote shouldn't steal focus.
  const prevQuoteCountRef = useRef(replyQuotes.length);
  useEffect(() => {
    if (!isMobileRef.current && replyQuotes.length > prevQuoteCountRef.current) {
      textareaRef.current?.focus();
    }
    prevQuoteCountRef.current = replyQuotes.length;
  }, [replyQuotes.length]);

  // Session skills (bundled + host-discovered) come from the snapshot
  // on bind and populate the suggestions menu as ``/skill-name``
  // entries alongside the built-ins.
  const skills = useChatStore((s) => s.skills);
  // ``/model`` writes ``conv.model_override`` (the same column the REPL's
  // ``/model`` and native pickers write). In-process harnesses re-resolve
  // it each turn; native wrappers expose it only when they have a picker
  // path that the runner can propagate without blocking the vendor TUI.
  const showModel = !isNativeWrapper || showModels;
  // /compact is only functional for native wrappers (claude-native,
  // codex-native) which inject the slash command into the terminal.
  // SDK harnesses (openai-agents-sdk, claude-sdk) don't support it yet.
  const showCompact = isNativeWrapper;
  const slashCommands = useMemo(
    () => buildSlashCommandMap(skills, showEffort, showModel, showCompact),
    [skills, showEffort, showModel, showCompact],
  );
  // Skills always need an optional argument fill-in so the user can
  // type extra context after the name; built-in commands keep their
  // existing fill/execute split.
  const slashCommandsWithArgs = useMemo(
    () => buildSlashCommandWithArgsSet(skills, showEffort, showModel),
    [skills, showEffort, showModel],
  );

  // Suggestions menu is open while the user is still typing the command
  // name — i.e. the value starts with "/" with no spaces yet (once a
  // space appears the command name is done and args follow) and no second
  // "/" (guards against file-path-like strings).
  const trimmedValue = value.trimStart();
  const menuOpen =
    trimmedValue.startsWith("/") &&
    !trimmedValue.slice(1).includes("/") &&
    !trimmedValue.includes(" ") &&
    files.length === 0;
  // Query = what the user typed after the leading "/".
  const menuQuery = menuOpen ? trimmedValue.slice(1) : "";
  // Tint the `/skill` token blue while the draft reads as a slash command, so
  // the command shape is signalled as the user types it.
  const composerIsCommand = files.length === 0 && isSlashCommandText(value);
  const toggleCodexPlanMode = async () => {
    if (planModeBusy) return;
    setCommandError(null);
    setPlanModeBusy(true);
    try {
      await useChatStore.getState().setCodexPlanMode(!codexPlanMode);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setCommandError(`Could not ${codexPlanMode ? "exit" : "enter"} Plan mode: ${message}`);
    } finally {
      setPlanModeBusy(false);
    }
  };
  // Filtered matches — kept in sync with what SlashCommandMenu renders so
  // keyboard nav indexes into the same list.
  const menuMatches = menuOpen
    ? Object.keys(slashCommands).filter((name) => name.slice(1).startsWith(menuQuery.toLowerCase()))
    : [];

  // Pre-select the first match whenever the filtered list changes — both
  // when the menu first opens (matches go [] → non-empty) and as the query
  // narrows it. Highlighting the top item is what lets Tab/Enter complete it
  // without the user arrowing down first; the keydown completion branch is
  // gated on ``menuIndex >= 0``. Arrow navigation only mutates ``menuIndex``
  // (not ``menuMatches``), so it never trips this reset.
  const prevMenuMatchesRef = useRef<string[]>([]);
  if (
    menuMatches.length !== prevMenuMatchesRef.current.length ||
    menuMatches.some((m, i) => m !== prevMenuMatchesRef.current[i])
  ) {
    prevMenuMatchesRef.current = menuMatches;
    setMenuIndex(menuMatches.length > 0 ? 0 : -1);
  }

  // "@"-mention is a drill-down file/folder browser. The token after "@"
  // doubles as a path: text up to the last "/" is the directory being
  // browsed; text after it filters that directory's entries. Opening a
  // folder rewrites the token to "<dir>/" so the menu re-lists it — that
  // is how nested files are reached (recursion via navigation, mirroring
  // the terminal's git-tracked walk). At any level the user can attach a
  // single file or the whole folder.
  const { dir: mentionDir, filter: mentionFilter } = parseMentionToken(mention?.query ?? "");
  // Root listing reuses the gate's useWorkspaceAllFiles; a sub-directory
  // uses the lazy per-dir hook (disabled — null path — at the root). Both
  // return files AND directories with a ``type`` discriminator.
  const mentionDirQuery = useWorkspaceDirectory(
    conversationId ?? undefined,
    mentionEnabled && mention && mentionDir ? mentionDir : null,
  );
  const mentionSourceEntries: WorkspaceFile[] = mentionDir
    ? (mentionDirQuery.data ?? [])
    : (workspaceFilesQuery.data?.data ?? []);
  // Folders first, filtered by the typed segment, capped (see rankMentionEntries).
  const mentionEntries: WorkspaceFile[] =
    mentionEnabled && mention ? rankMentionEntries(mentionSourceEntries, mentionFilter) : [];
  // True while a mention token is active but its listing hasn't resolved yet:
  // the cold-boot root fetch, or a sub-directory's first load after drilling
  // in. During this window ``mentionEntries`` is transiently empty (so the
  // menu is closed), and a stray Enter must NOT fall through to ``submit`` and
  // send the half-typed "@dir/" token as a chat message. A *settled*
  // zero-match (e.g. "@notafile") is deliberately excluded — sending that
  // literally is the user's intent.
  const mentionListingPending =
    mentionEnabled &&
    mention != null &&
    (mentionDir ? mentionDirQuery.isLoading : workspaceFilesQuery.isLoading);

  // Shared selection/chip/keyboard glue (see useMentionBrowser). The token
  // state and the data source above stay here; everything stateful is shared
  // so this composer and the launcher can't drift. ``setText`` also flags the
  // draft dirty so attach/drill participate in draft persistence.
  const {
    mentionIndex,
    mentionOpen,
    mentionedItems,
    setMentionedItems,
    attachMention,
    openMentionDir,
    removeMentionedItem,
    handleKeyDown: handleMentionKeyDown,
    dismiss: dismissMention,
  } = useMentionBrowser({
    mention,
    setMention,
    mentionEntries,
    text: value,
    setText: (next) => {
      setValue(next);
      dirtyRef.current = true;
    },
    textareaRef,
    isMobile,
  });

  // Depends on mentionedItems (from the hook above), so it's computed here.
  const hasDraft = value.trim().length > 0 || files.length > 0 || mentionedItems.length > 0;
  const showInterruptButton = isWorking && !hasDraft;

  // Drain externally-queued attachments (file viewer "Attach to agent") into
  // the local mention chips, deduping against what's already tagged, then
  // clear the store queue so they aren't re-applied. Placed after
  // ``useMentionBrowser`` since it owns ``setMentionedItems``.
  useEffect(() => {
    if (pendingComposerAttachments.length === 0) return;
    setMentionedItems((prev) => {
      // Dedup against already-tagged chips AND within this batch (accumulate
      // into ``seen`` as we go) so a duplicated queue can't double-apply.
      const seen = new Set(prev.map(composerAttachmentKey));
      const fresh: MentionItem[] = [];
      for (const a of pendingComposerAttachments) {
        const k = composerAttachmentKey(a);
        if (seen.has(k)) continue;
        seen.add(k);
        fresh.push(a);
      }
      return fresh.length > 0 ? [...prev, ...fresh] : prev;
    });
    useChatStore.getState().clearPendingComposerAttachments();
    textareaRef.current?.focus();
    // Defense-in-depth against the cross-session leak: if the composer unmounts
    // while an entry is still queued (route change, panel close, the
    // loading-conversation gate during a session switch), clear the queue so
    // the next-mounted composer doesn't drain a stale chip. ``switchTo`` also
    // resets the queue, but this closes the non-switch unmount paths too.
    return () => useChatStore.getState().clearPendingComposerAttachments();
    // setMentionedItems is a stable useState setter (from useMentionBrowser).
  }, [pendingComposerAttachments, setMentionedItems]);

  /**
   * Execute a slash command by name + optional argument string.
   * Clears the input and error state on success (or sets an error on
   * bad usage). Returns ``true`` when the command was recognised.
   */
  const executeSlashCommand = (cmd: string, arg: string): boolean => {
    switch (cmd) {
      case "/compact":
        if (!showCompact) {
          setCommandError("/compact is not supported for this agent type");
          return true;
        }
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        void useChatStore
          .getState()
          .compact()
          .catch((err: unknown) => {
            setCommandError(err instanceof Error ? err.message : "Compact failed");
          });
        return true;
      case "/effort": {
        if (!showEffort) return false;
        const valid = [...effortLevels, "default"];
        if (!arg || !valid.includes(arg.toLowerCase())) {
          setCommandError(`Usage: /effort ${valid.join(" | ")}`);
          return true;
        }
        const level = arg.toLowerCase() === "default" ? null : arg.toLowerCase();
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        void useChatStore
          .getState()
          .setEffort(level)
          .catch((err: unknown) => {
            setCommandError(err instanceof Error ? err.message : "Failed to set effort");
          });
        return true;
      }
      case "/model": {
        // The command guard checks only the "/model" token, so both bare
        // gateway ids ("databricks-gpt-5-4") and provider-prefixed ids
        // ("anthropic/claude-opus-4-8") reach here as the argument.
        if (!showModel) return false;
        const target = arg.trim();
        if (!target) {
          const { sessionModelOverride, llmModel } = useChatStore.getState();
          const current = sessionModelOverride
            ? `${sessionModelOverride} (override)`
            : (llmModel ?? "agent default");
          setCommandError(`Model: ${current}\nUsage: /model <name> · /model default to reset`);
          return true;
        }
        // ``default | off | reset`` clear the override (REPL clear aliases);
        // ``setModel(null)`` sends the server's "default" clear sentinel.
        const clear = ["default", "off", "reset"].includes(target.toLowerCase());
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        // Confirmation is a durable `[System: model changed to X]` note the
        // server appends to the transcript (see _persist_model_change_note) —
        // not a transient composer hint. Surface only failures inline here.
        void useChatStore
          .getState()
          .setModel(clear ? null : target)
          .catch((err: unknown) => {
            setCommandError(err instanceof Error ? err.message : "Failed to set model");
          });
        return true;
      }
      case "/context": {
        const state = useChatStore.getState();
        const { contextWindow, llmModel, sessionModelOverride, tokensUsed, blocks } = state;
        const lines: string[] = [];
        if (sessionModelOverride) lines.push(`Model: ${sessionModelOverride} (override)`);
        else if (llmModel) lines.push(`Model: ${llmModel}`);
        // contextWindow > 0 keeps a zero window out of the division (0/0 → "NaN%").
        if (tokensUsed != null && contextWindow != null && contextWindow > 0) {
          const pct = Math.min(tokensUsed / contextWindow, 1);
          const filled = Math.round(pct * 20);
          const bar = "█".repeat(filled) + "░".repeat(20 - filled);
          const pctStr = (pct * 100).toFixed(1);
          lines.push(
            `${tokensUsed.toLocaleString()} / ${contextWindow.toLocaleString()} tokens (${pctStr}%)`,
          );
          lines.push(bar);
        } else if (tokensUsed != null) {
          lines.push(`${tokensUsed.toLocaleString()} tokens`);
          lines.push("(Context window size unknown)");
        } else {
          lines.push("No usage data yet — send a message first.");
        }
        lines.push(`Items in context: ${blocks.length}`);
        setCommandError(lines.join("\n"));
        return true;
      }
      case "/help": {
        const lines = Object.entries(slashCommands).map(([name, desc]) => `${name} — ${desc}`);
        setCommandError(lines.join("\n"));
        return true;
      }
      default:
        setCommandError(
          `Unknown command: ${cmd}. Available: ${Object.keys(slashCommands).join(", ")}`,
        );
        return false;
    }
  };

  /**
   * Called when the user selects a suggestion from the menu (keyboard or
   * click). Commands that need an argument (``SLASH_COMMANDS_WITH_ARGS``)
   * fill in the text with a trailing space so the user can type the arg.
   * All other commands execute immediately.
   */
  const applyMenuSelection = (cmd: string) => {
    setMenuIndex(-1);
    if (slashCommandsWithArgs.has(cmd)) {
      // Fill in "cmd " and let the user type the argument.
      setValue(cmd + " ");
      dirtyRef.current = true;
      textareaRef.current?.focus();
    } else {
      // Execute immediately — no argument needed.
      setValue("");
      setCommandError(null);
      executeSlashCommand(cmd, "");
    }
  };

  // Auto-grow the textarea from 1 row up to 10 rows, then let it scroll.
  useAutoGrowTextarea(textareaRef, value);

  // Scope recall to the active conversation so ArrowUp surfaces only this
  // chat's prompts, not the last thing typed in any other chat.
  const { appendEntry, recallPrevious, recallNext, resetCursor } = usePromptHistory(conversationId);
  // Set just before recall sets `value`; cleared when the resulting onChange
  // fires. Lets onChange distinguish "user typed" (reset cursor) from
  // "recall replaced the value" (keep cursor).
  const recallingRef = useRef(false);

  const [isDragActive, setIsDragActive] = useState(false);

  const addFiles = (incoming: File[]) => {
    // Reject unsupported types (only images, PDF, and text/code) and
    // oversized files up front — before the upload — with a friendly
    // message. The server enforces the same limits authoritatively.
    const { accepted, errors } = validateAttachments(incoming);
    if (accepted.length > 0) {
      setFiles((prev) => [...prev, ...accepted]);
      dirtyRef.current = true;
      // Return focus to the composer so the user can keep typing right
      // after attaching (the file picker / paperclip button steals it).
      if (!isMobileRef.current) textareaRef.current?.focus();
    }
    setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(false);
    const dropped = Array.from(e.dataTransfer.files);
    if (dropped.length > 0) addFiles(dropped);
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragEnter = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragActive(true);
  };

  const handleDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    // Only clear the active state when the pointer leaves the container
    // itself, not when it moves between child elements inside it.
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsDragActive(false);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachmentError(null);
    dirtyRef.current = true;
  };

  const submit = () => {
    const trimmed = value.trim();
    // Allow send if there's text, attached files, OR "@"-tagged paths.
    if (
      (!trimmed && files.length === 0 && mentionedItems.length === 0) ||
      disabled ||
      hasPendingElicitation
    )
      return;

    // Slash command path: the first token must read as "/name" (the shared
    // isSlashCommandText guard — file paths like "/Users/foo/bar.txt" don't
    // match, while args after the name may carry paths or URLs, e.g.
    // "/review-pr https://github.com/...").
    // Commands don't mix with file attachments — require no files. Built-ins
    // run locally; a known skill routes through ``onSendSlashCommand`` (a
    // ``slash_command`` event) when that's wired — i.e. in-process sessions.
    // Anything else (unknown command, or a skill on a native-terminal
    // session where ``onSendSlashCommand`` is undefined) falls through to the
    // plaintext send path below.
    if (isSlashCommandText(trimmed) && files.length === 0 && mentionedItems.length === 0) {
      const parts = trimmed.split(/\s+/);
      const cmd = parts[0].toLowerCase();
      const arg = parts[1] ?? "";
      // Bare "/model" when the picker has a switchable Models section
      // (claude-native): sent as plaintext it would open Claude's interactive
      // selector inside the vendor TUI, which the web UI can't render — the
      // session just blocks. Open the composer's model picker instead and let
      // the user choose there. "/model <name>" takes the builtin route below to
      // setModel — the same write the picker makes.
      //
      // opencode is excluded: it surfaces showModels (the pill mirrors its live
      // TUI model) but ships no web model options, so intercepting bare "/model"
      // would pop an empty dropdown and swallow the command. Fall through to the
      // builtin "/model" handler below, which surfaces the current model as a
      // read-only hint. ("/model <name>" still routes to setModel there —
      // opencode reads model_override on the next web-injected turn.)
      if (cmd === "/model" && !arg && showModels && modelPickerKind !== "opencode") {
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        setPickerOpenNonce((n) => n + 1);
        return;
      }
      if (cmd in BUILTIN_SLASH_COMMANDS && cmd in slashCommands) {
        executeSlashCommand(cmd, arg);
        return;
      }
      // Known skill on an in-process session: send a `slash_command` event
      // (the REPL's wire shape) so the server resolves the skill and
      // injects its instructions, instead of the agent seeing the literal
      // "/name" text. `parts[0]` keeps the original case for the server's
      // exact-name lookup. `onSendSlashCommand` is undefined for
      // native-terminal sessions, so those fall through to the plaintext
      // path below and the vendor TUI loads the skill itself. Reply quotes
      // don't apply to a slash command (no content field) — clear them.
      if (onSendSlashCommand && parts[0] in slashCommands) {
        const skillArgs = trimmed.slice(parts[0].length).trim();
        appendEntry(trimmed);
        onSendSlashCommand(parts[0].slice(1), skillArgs);
        dirtyRef.current = true;
        setValue("");
        setCommandError(null);
        onClearAllQuotes();
        return;
      }
    }

    setCommandError(null);
    // Prepend all active reply quotes as Markdown blockquotes.
    const quotePreamble =
      replyQuotes.length > 0
        ? replyQuotes
            .map((q) =>
              q
                .split("\n")
                .map((line) => `> ${line}`)
                .join("\n"),
            )
            .join("\n\n") + "\n\n"
        : "";
    // Prepend each "@"-tagged path as an attachment marker on its own line —
    // the same format the native executors emit for attachments and that
    // title-seeding strips (_ATTACHMENT_MARKER_RE). Wording is harness-aware
    // (codex says "Attached file:"). Folders carry a trailing "/" so the
    // agent knows to open the directory. The native vendor reads the on-disk
    // workspace file/folder from this marker; no upload happens.
    const messageText =
      buildMentionPreamble(mentionedItems, sessionHarness) + quotePreamble + trimmed;
    // Sending while a prior response is streaming is fine — the
    // server queues the message and delivers it to the running task
    // (or starts a fresh one once the current drains). Escape still
    // interrupts.
    if (trimmed) appendEntry(trimmed);
    onSend(messageText, files.length > 0 ? files : undefined);
    dirtyRef.current = true;
    setValue("");
    setFiles([]);
    setAttachmentError(null);
    setMentionedItems([]);
    setMention(null);
    onClearAllQuotes();
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (showInterruptButton) {
      onStop();
      return;
    }
    submit();
  };

  const applyRecall = (ta: HTMLTextAreaElement, recalled: string) => {
    recallingRef.current = true;
    setValue(recalled);
    dirtyRef.current = true;
    // Move the caret to the end after React applies the new value. Without
    // this, the browser leaves the caret at its previous index, which can
    // land mid-word and feels broken.
    queueMicrotask(() => {
      ta.setSelectionRange(recalled.length, recalled.length);
    });
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isImeCompositionKeyEvent(e, isComposingRef.current)) {
      return;
    }

    // "@"-mention menu navigation (shared useMentionBrowser) — mutually
    // exclusive with the slash menu below (a mention token can't also read as a
    // "/"-command). Takes priority over history recall and submission.
    if (handleMentionKeyDown(e)) return;

    // When the suggestions menu is open, ArrowUp/Down navigate it and
    // Enter/Tab complete the highlighted item. These take priority over
    // history recall and normal submission.
    if (menuOpen && menuMatches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setMenuIndex((i) => (i + 1) % menuMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setMenuIndex((i) => (i <= 0 ? menuMatches.length - 1 : i - 1));
        return;
      }
      if ((e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !isMobile)) && menuIndex >= 0) {
        e.preventDefault();
        applyMenuSelection(menuMatches[menuIndex]!);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        // Dismiss the menu by clearing the input so the user can start fresh.
        setValue("");
        setMenuIndex(-1);
        return;
      }
    }

    // Enter sends; Shift+Enter inserts a newline. On mobile, Enter inserts a
    // newline (no Shift available on-screen) and Send must be tapped instead.
    if (e.key === "Enter" && !e.shiftKey && !isMobile && !e.nativeEvent.isComposing) {
      e.preventDefault();
      // The mention menu is briefly closed while its listing loads (see
      // ``mentionListingPending``); swallow Enter so the in-progress "@dir/"
      // token isn't sent as a chat message. The menu reopens when entries land.
      if (mentionListingPending) return;
      submit();
      return;
    }
    // Esc cancels an in-flight turn. When idle it's a no-op — clearing on
    // Esc destroys typed prompts with no undo (common muscle memory after
    // dismissing autocomplete suggestions).
    if (e.key === "Escape" && isStreaming) {
      e.preventDefault();
      onStop();
      return;
    }
    // ArrowUp/Down recall — only when the caret is already at the very
    // start (ArrowUp) or end (ArrowDown) of the text.  Checking for the
    // absence of "\n" before/after the cursor is not sufficient: long
    // single-line text that wraps visually contains no newlines, so that
    // check always fires and history recall intercepts cursor movement
    // within the wrapped line.  Gating on position 0 / length ensures the
    // browser gets to move the caret through wrapped lines first; only the
    // final ArrowUp-at-start / ArrowDown-at-end triggers recall.
    // Recall is for UNmodified arrows only. Cmd/Ctrl+↑/↓ (switch session) and
    // Cmd/Alt+↑/↓ (jump between messages) are global window hotkeys meant to
    // fire even mid-compose; without this guard the recall below intercepts
    // them (replacing the draft) and the hotkeys appear broken in the composer.
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const ta = e.currentTarget;
      if (e.key === "ArrowUp" && ta.selectionStart === 0) {
        const recalled = recallPrevious(value);
        if (recalled !== null) {
          e.preventDefault();
          applyRecall(ta, recalled);
        }
      } else if (e.key === "ArrowDown" && ta.selectionEnd === ta.value.length) {
        const recalled = recallNext();
        if (recalled !== null) {
          e.preventDefault();
          applyRecall(ta, recalled);
        }
      }
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const pastedFiles: File[] = [];
    for (const item of items) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) pastedFiles.push(file);
      }
    }
    if (pastedFiles.length > 0) {
      e.preventDefault();
      addFiles(pastedFiles);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className={cn(
        "chat-composer-form px-4 md:px-6",
        isTerminalFirst ? "terminal-first-composer-form pb-1.5" : "pb-3",
      )}
    >
      {/* Hidden file input for the attach button */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept="image/*,application/pdf,text/*,application/json"
        className="hidden"
        onChange={(e) => {
          if (e.target.files) {
            addFiles(Array.from(e.target.files));
            // Reset so the same file can be re-selected.
            e.target.value = "";
          }
        }}
      />
      {/* Queued messages — peeks above the card like the sub-agent tray.
          Lists follow-ups held while the agent is busy; drains FIFO on idle.
          Scope to this conversation so a queue held elsewhere never leaks in. */}
      <QueuedMessagesStrip
        messages={queuedMessages.filter((m) => m.conversationId === conversationId)}
        onDelete={dequeueMessage}
        onEdit={(queueId) => {
          // Pull the queued message back into the composer for editing:
          // replace the composer's text + attachments with the queued
          // message's, remove it from the queue, and focus the textarea.
          // Re-sending re-queues it (busy) or sends it (idle).
          const target = queuedMessages.find((m) => m.queueId === queueId);
          if (!target) return;
          setValue(target.text);
          setFiles(target.files ?? []);
          dequeueMessage(queueId);
          textareaRef.current?.focus();
        }}
        onSteer={(queueId) => steerMessage(queueId)}
        onReorder={reorderQueuedMessage}
        widthClassName={CHAT_COLUMN_WIDTH}
      />
      {/* Sub-agent context tray — peeks above the card; reserves its own
          layout slot so the card sits below it (see SubagentComposerTray).
          Truthy (not just non-null) so an empty label never peeks a
          nameless tray. */}
      {subAgentLabel ? <SubagentComposerTray label={subAgentLabel} /> : null}
      {/* Single rounded container — textarea on top, action row beneath.
          No top border on the surrounding form; the box itself is the
          visual container. The static neutral border carries through
          focus — no focus-within ring — so the box stays clean while
          typing. Drag-over still lifts an inset ring (below).
          dark:bg-card-solid: the trays tuck their square corners behind
          this card (-mb-4 / -mt-4), and the dark glass --card is 60%
          alpha — the tucked strips ghost through a translucent card. The
          glass rule still keys off the bg-card class, so the dark border/
          shadow chrome is unchanged; only the fill goes opaque. */}
      <div
        className={cn(
          "relative mx-auto flex w-full flex-col rounded-2xl border border-border bg-card dark:bg-card-solid shadow-sm transition",
          CHAT_COLUMN_WIDTH,
          isDragActive && "ring-2 ring-ring ring-inset",
        )}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragEnter={handleDragEnter}
        onDragLeave={handleDragLeave}
      >
        {isDragActive && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-card/80">
            <span className="text-sm font-medium text-ring">Drop files here</span>
          </div>
        )}
        {/* Slash-command suggestions — floats above the composer box */}
        {menuOpen && (
          <SlashCommandMenu
            query={menuQuery}
            activeIndex={menuIndex}
            onSelect={applyMenuSelection}
            commands={slashCommands}
          />
        )}
        {/* "@"-file-mention browser — native coding-agent sessions only.
            Also shown (as a loading row) while the listing is still fetching,
            so "@" isn't silently dead during runner cold-boot or a drill-in. */}
        {(mentionOpen || mentionListingPending) && (
          <FileMentionMenu
            currentDir={mentionDir}
            activeIndex={mentionIndex}
            entries={mentionEntries}
            loading={mentionListingPending}
            onOpenDir={openMentionDir}
            onAttach={attachMention}
          />
        )}
        {/* Quote chips — one per quoted selection, shown above the textarea */}
        {replyQuotes.length > 0 && (
          <div className="flex flex-col gap-1.5 px-4 pt-3 pb-0">
            {replyQuotes.map((quote, i) => (
              <div key={i} className="flex items-start gap-2">
                <div className="min-w-0 flex-1 bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-xs text-muted-foreground">
                  <span className="block truncate">
                    {quote.length > 120 ? `${quote.slice(0, 120)}…` : quote}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => onRemoveQuote(i)}
                  className="mt-0.5 shrink-0 rounded-full text-muted-foreground hover:text-foreground"
                  aria-label="Remove quote"
                >
                  <XIcon className="size-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
        {/* Highlight overlay: a textarea can only paint its text one color, so
            to tint just the `/skill` token we hide the textarea's own glyphs
            (text-transparent, caret kept visible) and render an aligned mirror
            behind it. Same box/typography so wrapping matches the textarea
            exactly. Only mounted while the draft is a command. */}
        <div className="relative">
          {composerIsCommand && (
            <div
              ref={backdropRef}
              aria-hidden
              data-testid="composer-highlight-overlay"
              className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-4 pt-3 pb-2 text-sm text-foreground"
            >
              {(() => {
                const split = splitSlashCommand(value);
                if (!split) return value;
                return (
                  <>
                    {split.before}
                    <span className="text-brand-accent">{split.token}</span>
                    {split.after}
                  </>
                );
              })()}
            </div>
          )}
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              dirtyRef.current = true;
              if (commandError !== null) setCommandError(null);
              // Recompute the active "@"-mention from the caret on every
              // keystroke (native coding-agent sessions — ``mentionEnabled``).
              setMention(
                mentionEnabled
                  ? detectMentionAt(
                      e.target.value,
                      e.target.selectionStart ?? e.target.value.length,
                    )
                  : null,
              );
              // Treat user-driven changes as exiting recall mode. Recall-
              // driven setValue toggles `recallingRef` first so we skip the
              // reset for that one tick.
              if (recallingRef.current) recallingRef.current = false;
              else resetCursor();
            }}
            onCompositionStart={() => {
              isComposingRef.current = true;
            }}
            onCompositionEnd={() => {
              isComposingRef.current = false;
            }}
            onKeyDown={handleKeyDown}
            onBlur={() => {
              // Dismiss the "@"-mention menu when focus leaves the textarea
              // (clicking a chip's ✕, the Send button, or another field).
              // Menu rows ``preventDefault`` on mousedown so selecting an entry
              // keeps focus and does NOT blur — this only fires for genuine
              // focus-out, where the lingering menu would otherwise float.
              dismissMention();
            }}
            onPaste={handlePaste}
            onScroll={(e) => {
              // Keep the overlay's scroll position locked to the textarea's.
              if (backdropRef.current) backdropRef.current.scrollTop = e.currentTarget.scrollTop;
            }}
            aria-label="Message the agent"
            placeholder={
              readOnlyReason !== null
                ? readOnlyReason
                : isReadOnly
                  ? "You have read-only access to this session"
                  : unreachable
                    ? "Session offline — reconnect below to continue"
                    : hasPendingElicitation
                      ? "Respond to the pending request above to continue"
                      : disabled
                        ? "Waiting for agents…"
                        : isStreaming
                          ? "Send a follow-up (queued) — Esc to stop"
                          : sandboxAsleepHint
                            ? "Current session's host is offline. Next message will resume the sandbox host which can take minutes"
                            : reconnectHint
                              ? "Send a message to reconnect this session"
                              : "Ask the agent anything…"
            }
            rows={1}
            disabled={disabled || isReadOnly || unreachable || hasPendingElicitation}
            data-slash-command={composerIsCommand ? "true" : undefined}
            className={cn(
              "relative w-full resize-none bg-transparent px-4 pt-3 pb-2 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-60",
              // Hand glyph painting to the overlay while a command is drafted;
              // the caret stays visible via caret-foreground.
              composerIsCommand && "text-transparent caret-foreground",
            )}
          />
        </div>
        {/* File chips — shown below textarea when files are attached */}
        {files.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-4 pb-2">
            {files.map((file, i) => (
              <span
                key={i}
                className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
              >
                {file.type.startsWith("image/") ? (
                  <ImageIcon className="size-3 shrink-0" />
                ) : (
                  <FileTextIcon className="size-3 shrink-0" />
                )}
                <span className="max-w-[140px] truncate">{file.name || "image.png"}</span>
                <button
                  type="button"
                  onClick={() => removeFile(i)}
                  className="ml-0.5 rounded-full hover:text-foreground"
                  aria-label={`Remove ${file.name || "image.png"}`}
                >
                  <XIcon className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}
        {/* Rejected-attachment feedback: unsupported type or too large */}
        {attachmentError !== null && (
          <div className="px-4 pb-2 text-xs text-destructive whitespace-pre-wrap">
            {attachmentError}
          </div>
        )}
        {/* "@"-mention chips — one per tagged workspace file/folder. Each is
            delivered as a "[Attached: <path>]" marker at send time. */}
        {mentionedItems.length > 0 && (
          <div className="flex flex-wrap gap-1.5 px-4 pb-2">
            {mentionedItems.map((item, i) => (
              <span
                key={mentionItemPath(item)}
                className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
              >
                {item.isDir ? (
                  <FolderIcon className="size-3 shrink-0" />
                ) : (
                  <FileTextIcon className="size-3 shrink-0" />
                )}
                <span className="max-w-[200px] truncate" title={mentionItemPath(item)}>
                  @{item.path}
                  {item.isDir ? "/" : ""}
                </span>
                {item.lineRange && (
                  <span className="shrink-0">
                    :{item.lineRange.start}-{item.lineRange.end}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => removeMentionedItem(i)}
                  className="ml-0.5 rounded-full hover:text-foreground"
                  aria-label={`Remove ${item.path}`}
                >
                  <XIcon className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}
        {/* Inline slash-command feedback: errors and /help output */}
        {commandError !== null && (
          <div className="px-4 pb-2 text-xs text-muted-foreground whitespace-pre-wrap">
            {commandError}
          </div>
        )}
        <div className="flex items-center justify-between gap-2 px-2 pb-2">
          {/* Attach + mic — left side of the action row */}
          <div className="flex shrink-0 items-center gap-0.5">
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="size-9 md:size-8"
              disabled={disabled || isReadOnly || hasPendingElicitation}
              onClick={() => fileInputRef.current?.click()}
              title="Attach files"
            >
              <PaperclipIcon className="size-4" />
              <span className="sr-only">Attach files</span>
            </Button>
            <ComposerMicButton
              disabled={disabled || isReadOnly || hasPendingElicitation}
              onTranscript={(text) => {
                setValue((prev) => (prev ? `${prev} ${text}` : text));
                dirtyRef.current = true;
                // Dictation is a user-driven edit — exit prompt-recall mode
                // so ArrowUp/ArrowDown don't clobber the dictated text.
                resetCursor();
                if (commandError !== null) setCommandError(null);
              }}
            />
          </div>
          {/* Cost toggle + agent picker + Send — right side */}
          <div className="flex min-w-0 items-center gap-0.5">
            {costRoutingEligible && (
              <IntelligentModelControl
                value={costControlModeOverride}
                onChange={(mode) =>
                  void useChatStore
                    .getState()
                    .setCostControlMode(mode)
                    .catch(() => {})
                }
                disabled={isReadOnly}
                verdict={costRoutingVerdict}
              />
            )}
            {showCodexPlanMode && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    size="sm"
                    variant={codexPlanMode ? "secondary" : "ghost"}
                    className={cn(
                      "h-9 gap-1.5 px-2 text-xs md:h-8",
                      codexPlanMode && "border border-ring/30 text-foreground",
                    )}
                    disabled={isReadOnly || planModeBusy}
                    aria-pressed={codexPlanMode}
                    aria-label={codexPlanMode ? "Exit Plan mode" : "Enter Plan mode"}
                    data-testid="codex-plan-mode-toggle"
                    data-active={codexPlanMode ? "true" : undefined}
                    onClick={() => void toggleCodexPlanMode()}
                  >
                    {planModeBusy ? (
                      <Loader2Icon className="size-3.5 animate-spin" />
                    ) : (
                      <FileTextIcon className="size-3.5" />
                    )}
                    <span>Plan</span>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {codexPlanMode ? "Exit Plan mode" : "Enter Plan mode"}
                </TooltipContent>
              </Tooltip>
            )}
            {showCodexGoal && (
              <CodexGoalControl
                conversationId={conversationId}
                readOnly={isReadOnly}
                goal={codexGoal}
                onGoalChange={setCodexGoal}
              />
            )}
            <AgentPicker
              agents={agents}
              isLoading={agentsLoading}
              selectedId={selectedAgentId}
              onSelect={onSelectAgent}
              effortLevels={effortLevels}
              showEffort={showEffort}
              modelPickerKind={modelPickerKind}
              codexModelOptions={codexModelOptions}
              disabled={isReadOnly}
              openNonce={pickerOpenNonce}
            />
            <Button
              type="submit"
              size="icon"
              variant={showInterruptButton ? "destructive" : "default"}
              // Send button fades more decisively when there's no draft —
              // overrides the base 50% disabled-opacity so the affordance
              // reads as "waiting for input", not "almost active".
              className={cn(
                "size-9 shrink-0 rounded-full md:size-8",
                !showInterruptButton && "hover:bg-primary/90 disabled:opacity-30",
              )}
              // Interrupt stays live during a pending elicitation —
              // cancelling the turn is the other legitimate way out.
              disabled={
                showInterruptButton
                  ? isReadOnly
                  : !hasDraft || disabled || isReadOnly || hasPendingElicitation
              }
              title={showInterruptButton ? "Interrupt" : "Send"}
              aria-label={showInterruptButton ? "Interrupt" : "Send"}
            >
              {showInterruptButton ? (
                <SquareIcon className="size-4 fill-current" />
              ) : (
                <ArrowUpIcon className="size-4" />
              )}
              <span className="sr-only">{showInterruptButton ? "Interrupt" : "Send"}</span>
            </Button>
          </div>
        </div>
      </div>
      <ComposerStatusLine
        harnessLabel={harnessLabel}
        codexGoal={codexGoal}
        isSubAgentSession={subAgentLabel != null}
      />
    </form>
  );
}

// The "Working…" shimmer tracks the server-side session status 1:1 with the
// status badge — no optimistic bridges. There is a brief gap after a send
// before the server confirms `running` (exactly like the badge); that's the
// intended behavior — the indicator reflects what the agent is actually doing.
export function computeIsWorking(sessionStatus: SessionStatus): boolean {
  return sessionStatus === "running" || sessionStatus === "waiting";
}

/**
 * Whether the main chat's display-only "Working…" indicator should light up.
 *
 * @param sessionStatus - The main session status, e.g. ``"running"``.
 * @param options - Display gates for the main chat indicator.
 * @param options.hasPendingElicitation - ``true`` when an elicitation prompt
 *   owns the in-progress slot and should suppress the shimmer/pinned pill.
 * @param options.runnerOnline - Runner liveness: ``true`` online, ``false``
 *   known offline, ``undefined`` before the health poll resolves. Only known
 *   offline suppresses the indicator.
 * @param options.backgroundTaskCount - Background shells still running after
 *   the turn ended. A claude-native turn settles to ``idle`` (the PTY-activity
 *   watcher's edge) even while shells run, so the bare status alone would hide
 *   the indicator; a positive count keeps it lit so "N background tasks still running"
 *   stays visible.
 * @returns ``true`` when the main session's own status should render Working.
 */
export function computeShowsWorking(
  sessionStatus: SessionStatus,
  options: {
    hasPendingElicitation: boolean;
    runnerOnline: boolean | undefined;
    backgroundTaskCount?: number;
  },
): boolean {
  if (options.runnerOnline === false) return false;
  if (options.hasPendingElicitation) return false;
  return computeIsWorking(sessionStatus) || (options.backgroundTaskCount ?? 0) > 0;
}

/**
 * Decide whether the carried initial prompt should be auto-sent now.
 *
 * The prompt is the optional first message the landing composer hands off via
 * the shared chatStore. It is sent exactly once per conversation, and only once
 * the session is ready: hydrated (snapshot loaded / stream bound) and an
 * agent resolved.
 *
 * We intentionally do NOT gate on runner liveness. The stream-bind gate
 * (``loadingConversation``) is load-bearing — the session stream is
 * live-tail with no replay buffer, so POSTing before ``bindStream``
 * connects would lose the turn's events ("no response"). But the runner
 * itself need not be online yet: the server's ``POST /events`` handler
 * holds the request open while a host-bound runner is spinning up (a 3s
 * connect grace, then a relaunch + 30s wait — see ``post_event`` in
 * ``sessions.py``), and only 503s if no runner ever comes online. So the
 * bubble (pushed synchronously by ``chatStore.send``) renders the moment
 * the stream binds, the server absorbs the runner race, and a genuinely
 * dead host surfaces as a failed send rather than a silently-dropped
 * prompt on an empty composer.
 *
 * @param params.initialPrompt Carried prompt, or ``null``/``""`` when
 *   none was passed, e.g. ``"read the README"``. Empty/falsy never sends.
 * @param params.promptConversationId The conversation id the prompt was
 *   consumed for, or ``null``. Must equal ``conversationId`` — a mismatch
 *   means the user switched sessions before the auto-send fired, so the
 *   prompt would leak into the now-active session.
 * @param params.sentForConversationId The conversation id the guard ref
 *   already auto-sent for, or ``null``. When it equals ``conversationId``
 *   the prompt was already dispatched for this session and must not
 *   resend; a different id (a later new chat reusing the mounted
 *   ChatPage) does not block.
 * @param params.conversationId Active session id from the URL, or
 *   ``null``/``undefined`` on the new-chat landing, e.g. ``"conv_abc"``.
 * @param params.loadingConversation ``true`` while the snapshot hydrates.
 * @param params.agentId Resolved agent id, or ``null`` before agents
 *   load, e.g. ``"ag_abc123"``.
 * @returns ``true`` only when every gate passes.
 */
export function shouldSendInitialPrompt(params: {
  initialPrompt: string | null;
  promptConversationId: string | null;
  sentForConversationId: string | null;
  conversationId: string | null | undefined;
  loadingConversation: boolean;
  agentId: string | null;
}): boolean {
  // Reject falsy (null or "") so a manipulated router state can't fire
  // send("") — defense-in-depth alongside the dialog's blank guard.
  if (!params.initialPrompt) return false;
  // The prompt must still belong to the active session. `initialPrompt` is
  // set by an effect whose `setInitialPrompt` doesn't flush until the next
  // render, so when the user switches `/c/:a` → `/c/:b` the auto-send effect
  // re-runs in the SWITCH commit with the STALE prompt (consumed for :a) but
  // the NEW conversationId (:b). send() then pins the live store id (already
  // :b) and the prompt leaks into the other session. Pinning the prompt to
  // the conversation it was consumed for closes that window.
  if (params.promptConversationId !== params.conversationId) return false;
  // Already dispatched for THIS conversation — don't resend. A different
  // (or null) id means a later new chat reusing the mounted ChatPage, so
  // it falls through and sends.
  if (params.sentForConversationId === params.conversationId) return false;
  if (!params.conversationId || params.loadingConversation || !params.agentId) {
    return false;
  }
  return true;
}

/**
 * Auto-send the landing composer's first message through the right wire
 * shape. A message the dialog matched to one of the agent's bundled
 * skills posts a ``slash_command`` event (the REPL's shape) so the
 * server resolves the skill — instead of the agent seeing literal
 * ``"/name"`` text. Everything else posts a plain message. The dialog
 * already kept ``skill`` null for native-terminal sessions (their CLI
 * owns slash commands) and for text that matched no bundled skill, so
 * both fall through to the plain path here. ``POST /events`` holds the
 * request while a host-bound runner boots, so the skill resolves
 * against the runner's real merged skill list once it registers.
 * Exported for unit testing.
 *
 * @param prompt The consumed pending prompt, e.g.
 *   ``{ text: "/review-pr 123", skill: { name: "review-pr", args: "123" } }``.
 * @param agentId Resolved agent id, e.g. ``"ag_abc123"``.
 * @param send ``chatStore.send`` — posts a plain user message. Always
 *   called with no files: the landing composer has no attachments.
 * @param sendSlashCommand ``chatStore.sendSlashCommand`` — posts a
 *   ``slash_command`` event.
 */
export function dispatchInitialPrompt(
  prompt: PendingInitialPrompt,
  agentId: string,
  send: (text: string, agentId: string, files: File[]) => Promise<void>,
  sendSlashCommand: (name: string, args: string, agentId: string) => Promise<void>,
): void {
  if (prompt.skill) {
    void sendSlashCommand(prompt.skill.name, prompt.skill.args, agentId);
  } else {
    void send(prompt.text, agentId, prompt.files ?? []);
  }
}

/**
 * Whether a session is an *unbound* coding fork — one that still needs the
 * directory picker to bind a host + workspace before it can run.
 *
 * The ``omnigent.fork.source_id`` label is *provenance*: it stays on the
 * clone forever, including after it is bound. So the label alone can't gate
 * the picker — a bound fork whose runner is merely offline would wrongly
 * open the picker, and the bind endpoint would 400 with "session already
 * has a runner bound". Gating additionally on an empty workspace mirrors the
 * server's ``needs_workspace`` connectivity flag (fork-source label present
 * AND ``workspace`` NULL): once the fork binds, ``workspace`` is set and this
 * returns false, routing an offline bound fork to the CLI reconnect dialog
 * like any other session.
 *
 * @param forkSourceId - The `omnigent.fork.source_id` label value, or null.
 * @param workspace - The session's bound workspace, or null/undefined when
 *   never bound.
 */
export function isUnboundCodingFork(params: {
  forkSourceId: string | null;
  workspace: string | null | undefined;
}): boolean {
  return params.forkSourceId !== null && !params.workspace;
}

const EFFORT_LEVELS = ["low", "medium", "high"] as const;

/** Anthropic-side efforts for claude-native sessions (matches ANTHROPIC_EFFORTS in reasoning_effort.py). */
const CLAUDE_NATIVE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

type NativeModelPickerKind = "claude" | "codex" | "cursor" | "kiro" | "opencode";

type LabelSource = { labels?: Record<string, string | null> | null } | null | undefined;

/**
 * Resolve a structural read-only reason from session labels.
 *
 * The live session snapshot is checked first because child sessions do
 * not appear in the sidebar list and because labels can change after
 * initial navigation (for example ``sys_session_close`` marks a child
 * ``omnigent.closed=true``). The sidebar row is only a fallback.
 *
 * @param activeSession - Live session snapshot, if loaded.
 * @param activeConv - Sidebar/session-list row fallback.
 * @returns Placeholder text for the composer when the session is
 *   structurally read-only, or ``null`` when normal permissions apply.
 */
export function readOnlyReasonForSessionLabels(
  activeSession: LabelSource,
  activeConv: LabelSource,
): string | null {
  const closed =
    activeSession?.labels?.["omnigent.closed"] ?? activeConv?.labels?.["omnigent.closed"];
  if (closed === "true") return "This sub-agent session is closed";
  const wrapper =
    activeSession?.labels?.["omnigent.wrapper"] ?? activeConv?.labels?.["omnigent.wrapper"];
  if (wrapper === "claude-code-native-ui-subagent") {
    return "Claude Code sub-agents are read-only";
  }
  return null;
}

export function effortLevelsForConv(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
  codexModelOptions: readonly CodexModelOption[] = [],
  currentModel: string | null = null,
): readonly string[] {
  switch (conv?.labels?.["omnigent.wrapper"]) {
    case "claude-code-native-ui":
      return CLAUDE_NATIVE_EFFORT_LEVELS;
    case "codex-native-ui":
      return codexEffortLevelsForModel(codexModelOptions, currentModel);
    default:
      return EFFORT_LEVELS;
  }
}

/**
 * Which native model picker should be visible for *conv*?
 *
 * Gated on the wrapper label, not `omnigent.ui === "terminal"`:
 * other terminal-first wrappers may not be Claude/Codex-native (see
 * `TerminalFirstContext.tsx`).
 */
export function modelPickerKindForConv(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): NativeModelPickerKind | null {
  switch (conv?.labels?.["omnigent.wrapper"]) {
    case "claude-code-native-ui":
      return "claude";
    case "codex-native-ui":
      return "codex";
    case "cursor-native-ui":
      return "cursor";
    case "kiro-native-ui":
      // Launch-only model selection: kiro applies ``--model`` at launch. Unlike
      // cursor/opencode there is no terminal->web model mirror, so the picker
      // reflects the pre-launch ``model_override`` selection.
      return "kiro";
    case "opencode-native-ui":
      // Like cursor: a vendor-owns-model wrapper that mirrors its live TUI
      // model into the session ``model_override`` (the forwarder's terminal→web
      // mirror), so the picker surfaces that as the live model.
      return "opencode";
    default:
      return null;
  }
}

export function shouldShowModelPicker(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return modelPickerKindForConv(conv) !== null;
}

/**
 * True when effort controls should be visible.
 *
 * :param conv: Session or sidebar row carrying labels. ``null`` or missing
 *     labels fail closed.
 * :returns: True only when the session supports Web UI effort controls.
 */
export function shouldShowEffortPicker(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return supportsEffortControl(conv);
}

export function shouldShowCodexPlanModeControl(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return isCodexNativeSession(conv);
}

/**
 * True when the Codex Goal control should be visible.
 *
 * @param conv - Session or sidebar row carrying labels. ``null`` or missing
 *   labels fail closed.
 * @returns True only for Codex-native wrapper sessions.
 */
export function shouldShowCodexGoalControl(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return isCodexNativeSession(conv);
}

/**
 * Highlight a model row when ``selectedModel`` is null by matching the
 * bound spec ``llmModel`` to its tier alias (e.g.
 * ``"anthropic/claude-opus-4-8"`` matches ``"opus"``).
 *
 * Sonnet 5 is special-cased both ways: its concrete id ("...-sonnet-5")
 * would otherwise substring-match the generic "sonnet" row (since "sonnet"
 * is itself a substring), and its own opt-in row id ("sonnet_5") never
 * literally appears in a hyphenated concrete id. The default "sonnet" row
 * stays bound to the older Sonnet (4.6), which collapses to it normally.
 */
export function isModelImplicitlySelected(modelId: string, llmModel: string | null): boolean {
  if (!llmModel) return false;
  const isSonnet5 = llmModel.includes("sonnet-5") || llmModel.includes("sonnet_5");
  if (modelId === "sonnet_5") return isSonnet5;
  if (modelId === "sonnet" && isSonnet5) return false;
  return llmModel === modelId || llmModel.endsWith(`/${modelId}`) || llmModel.includes(modelId);
}

interface AgentPickerProps {
  agents: Agent[] | undefined;
  isLoading: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  effortLevels: readonly string[];
  /** Show the Effort section and selected effort. */
  showEffort: boolean;
  /** Native model picker family, when present. */
  modelPickerKind: NativeModelPickerKind | null;
  /** Codex app-server model options for codex-native sessions. */
  codexModelOptions: readonly CodexModelOption[];
  /**
   * Disables the picker trigger. The picker is purely a write
   * surface (selecting an agent / model / effort changes how the
   * next turn runs), so read-only sessions disable it alongside the
   * other composer buttons.
   */
  disabled?: boolean;
  /**
   * External "open the dropdown" signal; nonce-keyed so repeat
   * requests re-open (same pattern as the composer prefill). Used by
   * bare ``/model`` submits. ``0`` / omitted means never requested.
   */
  openNonce?: number;
}

/** Title-case an effort level for the trigger pill (``"high"`` → ``"High"``). */
function formatEffortLabel(effort: string): string {
  return effort.charAt(0).toUpperCase() + effort.slice(1);
}

function AgentPicker({
  agents,
  isLoading,
  selectedId,
  onSelect,
  effortLevels,
  showEffort,
  modelPickerKind,
  codexModelOptions,
  disabled = false,
  openNonce = 0,
}: AgentPickerProps) {
  // Controlled so bare "/model" in the composer can open the dropdown.
  const [open, setOpen] = useState(false);
  const appliedOpenNonce = useRef(0);
  useEffect(() => {
    if (!openNonce || openNonce === appliedOpenNonce.current) return;
    appliedOpenNonce.current = openNonce;
    setOpen(true);
  }, [openNonce]);

  const hasAgents = !!agents && agents.length > 0;
  const selectedEffort = useChatStore((s) => s.selectedEffort);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const sessionModelOverride = useChatStore((s) => s.sessionModelOverride);
  const llmModel = useChatStore((s) => s.llmModel);

  // Codex and cursor both populate the picker from the server-provided
  // ``codexModelOptions`` channel (the snapshot's ``model_options`` field);
  // claude uses the static local catalog.
  const usesServerModelOptions =
    modelPickerKind === "codex" || modelPickerKind === "cursor" || modelPickerKind === "kiro";
  const modelOptions: ReadonlyArray<{ id: string; label?: string; displayName?: string }> =
    modelPickerKind === "claude"
      ? CLAUDE_NATIVE_MODELS
      : usesServerModelOptions
        ? codexModelOptions
        : [];
  const isNativeModelPicker = modelPickerKind !== null;
  // Only offer the agent list when there's an actual choice. Inside a
  // session the picker is scoped to the single bound agent (the runner is
  // tied 1:1 to it and can't be reassigned), so a one-row "Agents" section
  // is pure noise — drop it and let the dropdown be just the effort/model
  // controls.
  const showAgents = !isNativeModelPicker && (agents?.length ?? 0) > 1;
  const rawAgentName = agents?.find((a) => a.id === selectedId)?.name ?? agents?.[0]?.name;
  const agentDisplayName = rawAgentName ? agentDisplayLabel(rawAgentName) : rawAgentName;

  // The trigger now names what this control changes: model + effort. The
  // harness/agent identity moved to the status tray below the card.
  // qwen/goose/cursor/pi/opencode native wrappers pick their model inside
  // the vendor TUI, so the bound `llmModel` is an unused default — don't
  // surface it as if it were live; claude-/codex-native and SDK agents
  // resolve to a real model.
  const nativeVendorOwnsModel = useChatStore((s) => s.nativeVendorOwnsModel);
  // cursor-native is a vendor-owns-model wrapper, but unlike qwen/goose/pi/
  // opencode it mirrors its live TUI model into the session override
  // (`sessionModelOverride` / `model_override`), kept current both by the
  // forwarder's terminal→web mirror and by web-side picks. Surface *that* as
  // the live model — never the cross-session sticky `selectedModel` (a pick
  // carried over from some other session) nor the meaningless `llmModel`
  // default. The other vendor-owns wrappers have no Omnigent-visible model and
  // stay null.
  // kiro persists the pick as ``model_override`` (applied via ``--model`` at
  // launch) and, mid-session, the runner types ``/model <id>`` into the live TUI.
  // There is no terminal→web mirror, so the picker reflects the web-side
  // ``sessionModelOverride`` (which stays correct since a web pick sets it), like
  // cursor/opencode surface theirs.
  const pickerSelectedModel =
    modelPickerKind === "cursor" || modelPickerKind === "kiro" || modelPickerKind === "opencode"
      ? sessionModelOverride
      : selectedModel;
  // SDK/bundle agents (no native picker) never have the cross-session sticky
  // applied to them, so their live model is the session's own — the applied
  // override or the bound default — never `selectedModel` (a pick carried over
  // from an unrelated session, e.g. a gpt-5.5 left from a Codex session showing
  // on a Claude-SDK agent like Polly). claude-/codex-native keep `selectedModel`:
  // there the sticky IS the applied model.
  const nonNativeModel =
    modelPickerKind === null ? (sessionModelOverride ?? llmModel) : (selectedModel ?? llmModel);
  const effectiveModel = nativeVendorOwnsModel
    ? modelPickerKind === "cursor" || modelPickerKind === "kiro"
      ? // cursor mirrors its live TUI model into ``model_override``; kiro sets it
        // on a web pick (which also drives a live ``/model`` switch). Either way
        // the Omnigent-visible model is ``model_override``.
        sessionModelOverride
      : modelPickerKind === "opencode"
        ? // opencode mirrors its live TUI model into ``model_override`` (set at
          // launch and updated by the forwarder on a TUI switch); show that,
          // falling back to the launch-resolved model before any switch.
          (sessionModelOverride ?? llmModel)
        : null
    : nonNativeModel;
  const modelLabel = formatStatusModelLabel(effectiveModel, codexModelOptions);
  const effortTriggerLabel =
    showEffort && selectedEffort
      ? formatStatusEffortLabel(selectedEffort, modelPickerKind === "codex")
      : null;
  const hasPickerActions = showAgents || modelOptions.length > 0 || showEffort;

  // Until kiro mirrors its live model (its first session ``.json`` write), there
  // is no resolved model to show; fall back to the catalog default (e.g. "Auto")
  // so the trigger reads as a model rather than the harness name ("Kiro").
  const kiroDefaultOption =
    modelPickerKind === "kiro"
      ? (codexModelOptions.find((m) => m.isDefault) ?? codexModelOptions[0])
      : undefined;
  const kiroLaunchFallbackLabel = kiroDefaultOption?.displayName ?? kiroDefaultOption?.id;

  // Model in foreground (black), effort in muted (grey). Static fallbacks
  // first; the final `else` returns null so a session with nothing to show
  // and nothing to switch doesn't render an empty disabled pill — its
  // identity is carried by the status tray below.
  let triggerContent: React.ReactNode;
  if (isLoading) {
    triggerContent = "Loading…";
  } else if (!hasAgents) {
    triggerContent = "No agents";
  } else if (modelLabel) {
    triggerContent = (
      <>
        <span className="text-foreground">{modelLabel}</span>
        {effortTriggerLabel && <span className="text-muted-foreground"> {effortTriggerLabel}</span>}
      </>
    );
  } else if (effortTriggerLabel) {
    // Vendor owns the model but effort is still switchable from the web UI.
    triggerContent = <span className="text-muted-foreground">{effortTriggerLabel}</span>;
  } else if (showAgents) {
    // No model/effort to surface, but the user can still switch agents —
    // label the trigger with the current agent so the switcher reads clearly.
    triggerContent = agentDisplayName;
  } else if (hasPickerActions) {
    // The live model/effort isn't resolved yet (e.g. a claude-/codex-native
    // session before the snapshot fills llmModel/selectedEffort: the generated
    // spec may carry no executor model and no sticky/override is set), but the
    // dropdown still has model rows to switch. Keep the trigger rendered — and
    // the model dropdown + bare-`/model` open path reachable — with a stable
    // identity fallback rather than hiding the picker entirely. For kiro, prefer
    // the catalog default (e.g. "Auto") over the agent name so the launch-window
    // label reads as a model.
    triggerContent = kiroLaunchFallbackLabel ?? agentDisplayName ?? "Model";
  } else {
    return null;
  }

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!hasAgents || disabled || !hasPickerActions}
          data-testid="agent-picker-trigger"
          className="h-7 min-w-0 shrink gap-1.5 px-2 text-muted-foreground hover:text-foreground"
        >
          <span className="min-w-0 truncate text-xs tabular-nums">{triggerContent}</span>
          {hasPickerActions && <ChevronDownIcon className="size-3.5 shrink-0 opacity-60" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-64 p-1">
        {showAgents && (
          <>
            <PickerSectionHeader>Agents</PickerSectionHeader>
            {agents?.map((a) => (
              <DropdownMenuItem
                key={a.id}
                data-testid="agent-picker-item"
                data-agent-id={a.id}
                data-agent-name={a.name}
                data-active={a.id === selectedId ? "true" : undefined}
                onSelect={() => onSelect(a.id)}
                className={cn(
                  "items-start gap-2 rounded-sm px-2 py-1.5 text-xs",
                  "data-[active=true]:bg-accent/60 data-[active=true]:text-foreground",
                )}
              >
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="truncate">{agentDisplayLabel(a.name)}</span>
                  {a.description && (
                    <span className="truncate text-xs text-muted-foreground">{a.description}</span>
                  )}
                </div>
              </DropdownMenuItem>
            ))}
          </>
        )}
        {modelOptions.length > 0 && (
          <>
            {!isNativeModelPicker && <DropdownMenuSeparator className="my-1" />}
            <PickerSectionHeader>Models</PickerSectionHeader>
            {modelOptions.map((m) => {
              const isExplicit = pickerSelectedModel === m.id;
              const isImplicit =
                pickerSelectedModel === null &&
                (usesServerModelOptions
                  ? findCodexModelOption(codexModelOptions, llmModel)?.id === m.id
                  : isModelImplicitlySelected(m.id, llmModel));
              const isActive = isExplicit || isImplicit;
              return (
                <DropdownMenuItem
                  key={m.id}
                  data-testid="model-picker-item"
                  data-model-id={m.id}
                  data-active={isActive ? "true" : undefined}
                  onSelect={() =>
                    void useChatStore
                      .getState()
                      .setModel(m.id)
                      .catch(() => {})
                  }
                  className={cn(
                    "items-center gap-2 rounded-sm px-2 py-1.5 text-xs",
                    "data-[active=true]:bg-accent/60 data-[active=true]:text-foreground",
                  )}
                >
                  <span className="flex-1 truncate">
                    {usesServerModelOptions ? (m.displayName ?? m.id) : m.label}
                  </span>
                </DropdownMenuItem>
              );
            })}
          </>
        )}
        {/* Skip the leading rule when Effort is the only section, so the
            dropdown doesn't open with a stray divider at the top. */}
        {showEffort && (
          <>
            {(showAgents || modelOptions.length > 0) && <DropdownMenuSeparator className="my-1" />}
            <PickerSectionHeader>Effort</PickerSectionHeader>
            {effortLevels.map((level) => (
              <DropdownMenuItem
                key={level}
                data-testid="effort-picker-item"
                data-effort-level={level}
                data-active={selectedEffort === level ? "true" : undefined}
                onSelect={() =>
                  void useChatStore
                    .getState()
                    .setEffort(level)
                    .catch(() => {})
                }
                className={cn(
                  "items-center gap-2 rounded-sm px-2 py-1.5 text-xs",
                  modelPickerKind !== "codex" && "capitalize",
                  "data-[active=true]:bg-accent/60 data-[active=true]:text-foreground",
                )}
              >
                <span className="flex-1 truncate">{level}</span>
              </DropdownMenuItem>
            ))}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Category label inside the picker dropdown. Plain ``div``, not
 * ``DropdownMenuLabel``, so Radix doesn't claim focus for it.
 */
function PickerSectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2 pt-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
      {children}
    </div>
  );
}
