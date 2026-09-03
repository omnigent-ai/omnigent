import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArrowUpIcon,
  BotIcon,
  CornerUpLeftIcon,
  FileTextIcon,
  FolderIcon,
  GitBranchIcon,
  ImageIcon,
  Loader2Icon,
  PaperclipIcon,
  SettingsIcon,
  SquareIcon,
  SquareTerminalIcon,
  XIcon,
} from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  composerSendShortcutKeys,
  KeyboardShortcutTooltipContent,
} from "@/components/KeyboardShortcut";
import { useNavigate, useParams } from "@/lib/routing";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { Button } from "@/components/ui/button";
import { useAppName } from "@/lib/branding";
import { cn } from "@/lib/utils";
import { QueuedMessagesStrip } from "@/pages/QueuedMessagesStrip";
import { attachmentKey, validateAttachments } from "@/lib/attachments";
import {
  serverSwitcherHiddenForSurface,
  useSurfaceFrontmost,
} from "@/hooks/useNativeServerSwitcher";
import { isIOSShell, onNativeSidebarDrag, setNativeServerSwitcherHidden } from "@/lib/nativeBridge";
import { type Agent, useSessionAgent, useAgents } from "@/hooks/useAgents";
import { agentDisplayLabel } from "@/components/AgentInfo";
import {
  BRAIN_HARNESS_LABELS,
  SMART_ROUTING_LABEL,
  useBrainHarnessLabels,
} from "@/lib/agentLabels";
import { useConversations } from "@/hooks/useConversations";
import { usePermissions } from "@/hooks/usePermissions";
import type { NativeModelOption, Session, SessionStatus } from "@/lib/types";
import { usePromptHistory } from "@/hooks/usePromptHistory";
import { useAutoGrowTextarea } from "@/hooks/useAutoGrowTextarea";
import { useDictationInsert } from "@/hooks/useDictationInsert";
import {
  derivePermissionLevel,
  isOwnerLevel,
  isSessionSharedWithOthers,
} from "@/lib/permissionsApi";
import { getCurrentAuthorId } from "@/lib/identity";
import { retrySession } from "@/lib/sessionsApi";
import { codexEffortLevelsForModel, findNativeModelOption } from "@/lib/codexNativeModels";
import {
  composerAttachmentKey,
  consumePendingInitialPrompt,
  type PendingInitialPrompt,
  type QueuedMessage,
  useChatStore,
} from "@/store/chatStore";
import {
  isNativeTerminalSession,
  nativeCodingAgentForSession,
  nativeCodingAgentForHarness,
  nativeCodingAgentForSubagentWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import { readAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { isComposerSendKey, readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
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
import { getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
// Re-exported so existing tests importing these from "./ChatPage" keep working
// after the pure helpers moved to the shared lib.
export { detectMentionAt, mentionMarkerFor };
export type { MentionItem, MentionState };
// Bubble rendering, scroll helpers, and the working-indicator cluster moved to
// chatBubbleParts to break the ChatPage ↔ Transcript cycle. Re-exported so
// existing importers (tests + components) that reach them via "./ChatPage"
// keep working. SessionSharedContext and computeIsWorking are also imported
// back below for ChatPage's own use.
export {
  BubbleView,
  ConversationScrollRefBridge,
  HistoryAutoLoader,
  HistoryLoadingIndicator,
  JumpToTopButton,
  KeepBottomOnViewportResize,
  LatestTurnSpacer,
  ScrollToBottomOnSend,
  SessionSharedContext,
  UserMessageNavConnected,
  WORKING_MESSAGES,
  WorkingIndicator,
  bubbleKey,
  buildPendingBubbles,
  collectBubbleMarkdown,
  collectPendingElicitations,
  computeIsWorking,
  containsDisplayMath,
  containsMarkdownTable,
  extractUserText,
  isBackgroundTasksOnly,
  isSystemBubble,
  mergePendingBubbles,
  reorderCommittedRequestElicitations,
  shouldShowAuthorBadge,
  shouldShowWorkingIndicator,
  stripGatedSubagentRoutingChips,
  stripPendingElicitations,
  workingIndicatorLabel,
} from "@/components/chat/chatBubbleParts";
export type { ConversationScroller } from "@/components/chat/chatBubbleParts";
import {
  type ConversationScroller,
  SessionSharedContext,
  computeIsWorking,
} from "@/components/chat/chatBubbleParts";
import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { useSession } from "@/hooks/useSession";
import { useGithubInfo } from "@/hooks/useGithub";
import { useOpenGithubTab } from "@/shell/FileViewerContext";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useRefreshSessionStateOnRunnerOnline } from "@/hooks/useSessionOnlineRefresh";
import {
  type LivenessRow,
  type SessionLiveness,
  IMPORT_SOURCE_LABEL_KEY,
  livenessRowFromSession,
  useSessionLiveness,
} from "@/hooks/useSessionLiveness";
import { useMarkConversationSeen } from "@/hooks/useUnseenConversations";
import { useFileDropTarget } from "@/hooks/useFileDropTarget";
import { HostBadge } from "@/components/HostBadge";
import {
  BUILTIN_SLASH_COMMANDS,
  isSlashCommandText,
  rankedSlashCommandNames,
  SlashCommandMenu,
} from "@/components/SlashCommandMenu";
import { FileMentionMenu } from "@/components/FileMentionMenu";
import { FileDropOverlay } from "@/components/FileDropOverlay";
import {
  useWorkspaceAllFiles,
  useWorkspaceDirectory,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { ComposerMicButton } from "@/components/ComposerMicButton";
import { isCostRoutingSession, isSubagentRoutingSession } from "@/components/CostRoutingControl";
import {
  SMART_ROUTING_ARMS,
  hostBacksHarnessWithGateway,
  smartRoutingSourceFor,
} from "@/lib/smartRoutingAvailability";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ConfigRow,
  EFFORT_SELECT_NONE,
  EFFORT_UNAVAILABLE_PLACEHOLDER,
  MODEL_SELECT_DEFAULT,
  MODEL_SELECT_SMART,
  RoutingModelSelect,
  defaultModelLabel,
  nativeModelLabel,
} from "@/components/HarnessConfigControls";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { MainTerminalView } from "@/shell/MainTerminalView";
import { UNTITLED_CONVERSATION_LABEL } from "@/shell/sidebarNav";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { ResumeWithDirectoryDialog } from "@/shell/ResumeWithDirectoryDialog";
import { ReconnectSessionDialog } from "@/shell/ReconnectSessionDialog";
import { useTerminalFirst } from "@/shell/TerminalFirstContext";
import { supportsEffortControl } from "@/lib/sessionCapabilities";
import {
  CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
  claudePermissionModeLabel,
  isClaudeNativeSession,
} from "@/lib/claudePermissionMode";
import { isCodexNativeSession } from "@/lib/codexPlanMode";
import { getCliServerUrl } from "@/lib/host";
import { useOmnigentAnalytics } from "@/lib/analyticsEmit";
import { GoalControl, GoalStatusPill, useGoalState, type Goal } from "@/components/goal";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { ConnectionIndicator } from "./ChatIndicators";
import { CHAT_COLUMN_WIDTH } from "./chatLayout";
import { Transcript } from "@/components/chat/Transcript";

/** Server-info as consumers see it: the probe's result, or "loading". */
type ServerInfoValue = ServerInfo | "loading";

/**
 * Whether the deployment has smart routing at all. `"loading"` (the `/v1/info`
 * probe still in flight) reads as off, so no routing control flashes in and
 * then disappears on a server that has none.
 *
 * Source-agnostic: the gates below key on `smart_routing_enabled` alone because
 * which router answers doesn't change whether a session can be routed.
 */
function smartRoutingEnabled(serverInfo: ServerInfoValue): boolean {
  return serverInfo !== "loading" && serverInfo.smart_routing_enabled;
}

/**
 * Whether the session's own model can be routed per turn.
 *
 * SDK/bundle agent sessions need only the deployment flag. Native Claude
 * Code / Codex panes ARE routable per turn — the server injects the routed
 * pick via ``/model`` when ``cost_control_mode_override`` is on, the same
 * apparatus the create-time gear arms — but only when a router can answer
 * for their family (the server rejects a routing-on create otherwise): the
 * external AI-Gateway router needs the family's inference gateway-backed on
 * the session's host, and the built-in judge covers the rest. An absent
 * host row reads as backed, mirroring {@link hostBacksHarnessWithGateway}.
 */
export function isCostRoutingEligible(
  serverInfo: ServerInfoValue,
  session: Session | null | undefined,
  host?: { gateway_inference?: Record<string, boolean> | null } | null,
): boolean {
  if (serverInfo === "loading" || !serverInfo.smart_routing_enabled) return false;
  if (!isCostRoutingSession(session)) return false;
  if (!isNativeTerminalSession(session)) return true;
  const native = nativeCodingAgentForSession(session);
  if (native === undefined || !SMART_ROUTING_ARMS.some((arm) => arm === native.harness)) {
    return false;
  }
  return (
    smartRoutingSourceFor({
      externalConfigured: serverInfo.smart_routing_sources.external,
      ossConfigured: serverInfo.smart_routing_sources.oss,
      gatewayBacked: hostBacksHarnessWithGateway(host, native.harness),
    }) !== null
  );
}

/**
 * Whether the session may control the routing of the sub-agents it spawns —
 * same deployment flag, wider session gate (native Claude/Codex included, and
 * every non-native top-level agent session regardless of harness).
 */
export function isSubagentRoutingEligible(
  serverInfo: ServerInfoValue,
  session: Session | null | undefined,
): boolean {
  return smartRoutingEnabled(serverInfo) && isSubagentRoutingSession(session);
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

/**
 * Whether a submitted message should be queued rather than POSTed now.
 *
 * Queue when busy, or when this conversation already has a queued message even
 * if it reads idle: the direct-send and queue-drain paths aren't ordered, so a
 * later direct send could overtake a still-queued earlier one when status
 * flickers idle mid-queue (cursor-native). A new chat always sends.
 *
 * ``waiting`` is NOT busy for queueing: it means the turn already ended and the
 * agent loop is only parked on background work (background shells / sub-agents)
 * — the server's turn gate is already free, so a new message starts a fresh
 * turn immediately instead of stalling behind that background work. (The
 * "Working…" spinner and sidebar dot still treat ``waiting`` as active — those
 * reflect background activity, which is a separate concern from send gating.)
 *
 * ``alwaysSteer`` (a per-device preference) drops the busy gate entirely: a
 * follow-up sent mid-turn is POSTed now — steered into the running turn —
 * instead of parking in the queue strip. The ``hasQueued`` guard still holds:
 * once this conversation has a queued message it must drain in order, or a
 * direct send could overtake a still-queued earlier one on an idle flicker.
 */
export function shouldQueueSend(
  conversationId: string | null,
  status: "idle" | "streaming",
  sessionStatus: SessionStatus,
  queuedMessages: QueuedMessage[],
  alwaysSteer = false,
): boolean {
  if (conversationId === null) return false;
  const hasQueued = queuedMessages.some((m) => m.conversationId === conversationId);
  if (alwaysSteer) return hasQueued;
  const isBusy = status === "streaming" || sessionStatus === "running";
  return isBusy || hasQueued;
}

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
  const appName = useAppName();
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
    error: agentsError,
    refetch: refetchAgents,
  } = useAgents({ enabled: !urlConvId });
  const { data: conversationsData } = useConversations("", true);
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
  //
  // The streaming-hot fields (`blocks`, `activeResponse`,
  // `pendingUserMessages`, `interruptedResponseIds`) are NOT subscribed here:
  // they live in <Transcript>, so an SSE frame re-renders that subtree alone
  // and this root (and the composer/chrome it feeds) bails out. See
  // `hasPendingElicitation` below for the one blocks-derived value the root
  // still needs, read through an edge-stable boolean selector.
  const status = useChatStore((s) => s.status);
  const sandboxStatus = useChatStore((s) => s.sandboxStatus);
  // True while the session's managed-sandbox launch is still running
  // (a failed launch is NOT "launching" — it gets normal unreachable
  // handling). Overrides the liveness-derived unreachable affordances
  // below, which misread the not-yet-host-bound session as stranded.
  const sandboxLaunching = sandboxStatus !== null && sandboxStatus.stage !== "failed";
  // Terminal-first spin-up state, read here (not just in the child surfaces) so
  // the working-indicator gate below can defer to the "Starting up…" cue.
  const chatTerminalFirst = useTerminalFirst();
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

  // Picker selection. ChatPage stays mounted across `/` to `/c/:id`,
  // so the pick survives sidebar clicks; resets on full page reload.
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const agentId = selectedAgentId ?? agents?.[0]?.id ?? null;

  // Sync the picker to the conversation's bound agent when switching.
  // `boundAgentId` is `null` on `/`, during the snapshot fetch, and
  // for legacy conversations without an agent binding — leave the
  // picker alone in those cases.
  //
  // On the landing page, if the bound agent isn't in the cached list
  // (e.g. a new agent registered by a fresh `omnigent run` after load),
  // refetch on demand — useAgents is only enabled there.
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
  // sit before the early-return guards below (Rules of Hooks). Read through
  // a boolean selector (not the whole `blocks` array): Zustand bails out when
  // the flag is unchanged, so `blocks` reference churn on every streaming
  // frame no longer re-renders this root — only an elicitation edge does.
  const hasPendingElicitation = useChatStore((s) =>
    s.blocks.some((b) => b.type === "elicitation" && b.status === "pending"),
  );

  // Single-session snapshot (shared cache with chatStore.bindStream).
  // Must be declared BEFORE the early-return guards below — otherwise
  // the hook is skipped on renders that hit the loading/error branches,
  // tripping React's "rendered fewer hooks than expected".
  const { session: activeSession, isLoading: sessionLoading } = useSession(urlConvId ?? null);

  // Orchestrator-only: polly's children inherit its agentName, so the gate
  // needs the session predicate (parent linkage), not a bare name check. An
  // eligible session's Smart Routing option lives in the gear modal — Claude
  // Code / Codex fold it into the Model dropdown (the server routes native
  // panes per turn via /model injection); other routable agents get a
  // standalone Switch row. The session's host row feeds the per-family
  // gateway check the external router requires.
  const serverInfo = useServerInfo();
  const { data: hostRows } = useHosts();
  const sessionHost =
    hostRows?.find((row) => row.host_id === (activeSession?.hostId ?? null)) ?? null;
  const costRoutingEligible = isCostRoutingEligible(serverInfo, activeSession, sessionHost);
  // Sub-agent routing is a separate knob with a different gate: a native CLI
  // can't per-turn route itself, but the sub-agents it spawns are routed per
  // spawn — where the launch actually installed that apparatus. See
  // isSubagentRoutingSession for which classes qualify; for native sessions
  // this is their only routing control.
  const subagentRoutingEligible = isSubagentRoutingEligible(serverInfo, activeSession);

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
  // A spin-up in flight owns the in-progress slot with more specific copy
  // ("Starting up…" / "Cloning repository…") than the generic shimmer, and
  // `RunnerStartingIndicator` only renders when the shimmer is absent. So the
  // OPTIMISTIC path must stand down here: a send that has to boot a runner is
  // exactly when the user needs to know it's booting, not just that we asked.
  // A server-confirmed `running`/`waiting` still wins — by then the harness is
  // up and the spin-up cue has self-gated to null.
  const spinUpInFlight =
    sandboxLaunching ||
    Boolean(chatTerminalFirst?.isTerminalFirst && chatTerminalFirst.terminalStartingUp);
  const showsWorking = computeShowsWorking(sessionStatus, {
    hasPendingElicitation,
    runnerOnline,
    backgroundTaskCount,
    // Optimistic: light up the moment this client dispatches, without waiting
    // for the server's ``running``. The sidebar row already reads this same
    // flag (``isStartingUp`` in Sidebar.tsx), so the two agreed only once the
    // server confirmed; now they agree immediately.
    localSendInFlight: status === "streaming" && !spinUpInFlight,
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
  // An unbound session (no host, no runner — e.g. an imported one) routes the
  // offline guard to the directory picker (bind + launch a runner on a chosen
  // machine) instead of the terminal reconnect dead-end — but only when that
  // resume would actually work. The picker calls launch_runner, which requires
  // the caller to OWN the session (a shared non-owner 404s), and — for imports —
  // only harnesses that reconstruct context from the omnigent transcript carry
  // onto a chosen host (kimi can't resume at all; kiro/qwen resume from a local
  // file that lives on the original machine). Everything else falls through to
  // the reconnect path rather than a picker that would fail or start blank.
  // See unboundSessionResumableInApp. The picker itself handles the no-online-
  // host case, so we don't gate on host availability here.
  const importSource =
    activeSession?.labels?.[IMPORT_SOURCE_LABEL_KEY] ??
    activeConv?.labels?.[IMPORT_SOURCE_LABEL_KEY] ??
    null;
  const canResumeOnLocalHost = unboundSessionResumableInApp({
    unbound:
      (activeSession?.hostId ?? activeConv?.host_id ?? null) === null &&
      (activeSession?.runnerId ?? activeConv?.runner_id ?? null) === null,
    isOwner: isOwnerLevel(activeSession?.permissionLevel ?? activeConv?.permission_level ?? null),
    importSource,
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
  //
  // Also prefer the snapshot's `permissionLevel` over the sidebar row's when
  // it's resolved: the hook derives `host_offline`'s `isOwner` from this
  // level, and a deployment whose session list is owner-only (the caller's
  // effective level omitted, e.g. the Databricks-managed server) leaves the
  // row's `permission_level` null — which would read permissively as "owner"
  // and offer a non-owner the host-reconnect path. The single-session
  // snapshot always carries the authoritative level.
  const livenessRow: LivenessRow | null = activeConv
    ? {
        ...activeConv,
        permission_level: activeSession?.permissionLevel ?? activeConv.permission_level,
        host_resumable: activeSession?.hostResumable ?? false,
        kind: activeSession?.kind,
        // Skip the cold-boot grace for imports (nothing is booting) so the
        // resume picker shows at once. Snapshot labels win; sidebar row is the
        // fallback for an off-page session.
        imported: Boolean((activeSession?.labels ?? activeConv.labels)?.[IMPORT_SOURCE_LABEL_KEY]),
      }
    : livenessRowFromSession(activeSession);
  // Host-switch launch marker; see the store field. Keeps this surface's
  // liveness in step with AppShell's, which drives the startup spinner.
  const runnerLaunchedAt = useChatStore((s) => s.runnerLaunchedAt);
  const liveness = useSessionLiveness(urlConvId ?? undefined, livenessRow, {
    turnActive: status === "streaming",
    launchedAt: runnerLaunchedAt,
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
    const fallback = urlConvId ? UNTITLED_CONVERSATION_LABEL : appName;
    const base = truncateTitle(activeConv?.title ?? subAgentTabTitle ?? fallback);
    document.title = showsWorking ? `● ${base}` : base;
  }, [activeConv?.title, subAgentTabTitle, showsWorking, urlConvId, appName]);

  const sessionModelOptions = useChatStore((s) => s.codexModelOptions);
  const selectedModel = useChatStore((s) => s.selectedModel);
  const llmModel = useChatStore((s) => s.llmModel);
  // Pre-catalog fallback: a fresh native session's own catalog only arrives
  // once its CLI is up (codex answers model/list after app-server boot,
  // ~15s cold), which left the gear's Model list sparse and its Effort row
  // hidden until then. The session's host already probed the same harness
  // for the new-chat picker — ride those cached rows (same ids the launch
  // accepts, ~90ms warm) until the runner's per-session catalog lands;
  // the runner truth replaces them the moment it arrives. Must stay above
  // the hydration early-returns below (hook order).
  const fallbackPickerKind = modelPickerKindForConv({
    labels: activeSession ? (activeSession.labels ?? {}) : (activeConv?.labels ?? {}),
    harness: activeSession?.harness ?? null,
  });
  const hostProbeHarness =
    fallbackPickerKind === "codex"
      ? "codex-native"
      : fallbackPickerKind === "claude"
        ? "claude-native"
        : null;
  const { data: hostProbeOptions } = useHostModelOptions(
    activeSession?.hostId ?? null,
    hostProbeHarness ?? "",
    hostProbeHarness !== null && sessionModelOptions.length === 0,
  );
  // Identity-stable on purpose: substitute only when the host rows actually
  // exist, else keep the store's own array reference — a fresh [] here would
  // re-render every options consumer (composer, gear, agent-info popover) on
  // each streaming/liveness tick.
  const codexModelOptions =
    sessionModelOptions.length === 0 && hostProbeOptions != null && hostProbeOptions.length > 0
      ? hostProbeOptions
      : sessionModelOptions;

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
    if (urlConvId && runnerOnline === false && (isUnboundFork || canResumeOnLocalHost)) {
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
    // FIFO immediately when genuinely idle, so nothing stalls. With the
    // always-steer preference on, a mid-turn follow-up skips the queue and is
    // POSTed now instead.
    const chat = useChatStore.getState();
    if (
      shouldQueueSend(
        chat.conversationId,
        chat.status,
        chat.sessionStatus,
        chat.queuedMessages,
        readAlwaysSteer(),
      )
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
    if (urlConvId && runnerOnline === false && (isUnboundFork || canResumeOnLocalHost)) {
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
    harness: activeSession?.harness ?? null,
  };
  const modelPickerKind = modelPickerKindForConv(capabilitySource);
  // Effort ladders key on the model the session is actually on — the
  // reported `llmModel` — falling back to the sticky preference only
  // before the first report lands.
  const effortLevels = effortLevelsForConv(
    capabilitySource,
    codexModelOptions,
    llmModel ?? selectedModel,
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
        // unbound coding clone or a host-less session the caller can resume
        // in-app opens the directory picker (bind + launch), everything else
        // gets the reconnect dialog.
        if (isUnboundFork || canResumeOnLocalHost) setResumeDirDialogOpen(true);
        else setReconnectDialogOpen(true);
      }}
      agents={visibleAgents}
      selectedAgentId={agentId}
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
      showClaudePermissionMode={shouldShowClaudePermissionModeControl(capabilitySource)}
      showGoalControl={shouldShowGoalControl(capabilitySource)}
      showClaudeGoalControl={shouldShowPollyClaudeGoalControl(activeSession)}
      showPollyCodexGoalControl={shouldShowPollyCodexGoalControl(activeSession)}
      costRoutingEligible={costRoutingEligible}
      subagentRoutingEligible={subagentRoutingEligible}
      subAgentLabel={subAgentLabel}
      wrapperLabel={capabilitySource.labels[WRAPPER_LABEL_KEY] ?? null}
    />
  );

  // On `/` (no conversation), the composer would let the user POST a
  // first message and silently create a session — but sessions are
  // bound 1:1 to a local runner that only the CLI can launch. Show
  // the CLI instructions instead so users learn the right entry point.
  if (!urlConvId) return <NewChatLandingScreen />;

  // Pick the reconnect dialog's state from the session's host binding, not
  // from liveness: the dialog opens both from an unreachable send AND from the
  // composer's host badge, which offers reconnect whenever the host tunnel is
  // down — including states liveness still calls reachable (a runner that
  // outlived its host). A host-bound session always reconnects via
  // `omnigent host`; only an unbound one relaunches locally. Ownership gates
  // the host command — a non-owner can't reach that machine.
  const hostBound = !!(activeSession?.hostId ?? activeConv?.host_id);
  const reconnectState = hostBound ? "host_offline" : "local_stranded";
  const reconnectIsOwner = isOwnerLevel(permissionLevel);

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
      {((isUnboundFork && forkSourceId) || canResumeOnLocalHost) && (
        <ResumeWithDirectoryDialog
          open={resumeDirDialogOpen}
          onOpenChange={setResumeDirDialogOpen}
          sessionId={urlConvId}
          // Fork clone prefills from its source; a host-less session has none
          // and prefills from its own recorded host/workspace/branch instead.
          sourceSessionId={isUnboundFork ? forkSourceId : null}
          prefill={{
            hostId: activeSession?.hostId ?? null,
            workspace: activeSession?.workspace ?? null,
            gitBranch: activeSession?.gitBranch ?? null,
          }}
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
 *
 * The embedded browser pane is NOT here — it lives as the "Browser"
 * tab inside the right Workspace rail (WorkspacePanel), so it never floats as a
 * mid-page column.
 */
function SessionLayout({ mainAgent }: SessionLayoutProps) {
  return (
    <div className="flex min-h-0 flex-1 overflow-hidden">
      {/* `relative`: positions MainAgentSurface's persistent terminal overlay
          and the composer's file-drop overlay (both absolute inset-0) against
          the main column. */}
      <div data-chat-surface className="relative flex min-w-0 flex-1 flex-col">
        {mainAgent}
      </div>
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
  status: "idle" | "streaming";
  /** Local stream OR cross-client `session.status: running`. Gates the
   *  composer's Stop/Interrupt button — the parent's OWN turn only. */
  isWorking: boolean;
  /** Display-only main-chat indicator after elicitation/offline gates.
   *  Never includes child-session activity and never gates Stop/Interrupt. */
  showsWorking: boolean;
  /**
   * Strict runner-tunnel liveness. The terminal view remains selectable when
   * this is false and uses it to show the stopped-harness resume state.
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
  selectedAgentId: string | null;
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
  /** Runner-owned model picker rows for native sessions. */
  codexModelOptions: readonly NativeModelOption[];
  /** Show the Codex Plan-mode toggle. */
  showCodexPlanMode: boolean;
  showClaudePermissionMode?: boolean;
  /** Show the session Goal control. */
  showGoalControl?: boolean;
  /** Show Polly's Claude SDK command-backed Goal control. */
  showClaudeGoalControl?: boolean;
  /** Show Polly's Codex command-backed Goal control. */
  showPollyCodexGoalControl?: boolean;
  /** Session passes `isCostRoutingSession` (polly orchestrator, not a child). */
  costRoutingEligible: boolean;
  /** Session passes `isSubagentRoutingSession` (top-level, native Claude/Codex or non-native). */
  subagentRoutingEligible: boolean;
  /**
   * Sub-agent instance label when the active session is a child, e.g.
   * ``"check-account-eligibility"``; ``null`` for top-level sessions.
   * Drives the composer's "Chatting with sub-agent …" tray and suppresses
   * the scroll-pinned "Working…" tab (the tray takes that slot). See
   * ``subAgentComposerLabel``.
   */
  subAgentLabel: string | null;
  /** The session's ``omnigent.wrapper`` label; see ``ComposerProps``. */
  wrapperLabel: string | null;
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
 * Whether the terminal surface should be MOUNTED (kept alive), as opposed
 * to shown. Broader than {@link shouldShowTerminalSurface}: once a
 * terminal is reachable, the surface mounts hidden behind the chat view
 * so the WS attach pre-warms in the background and survives Chat/Terminal
 * flips — making both the first open and every return near-instant. With
 * no reachable terminal the surface still mounts while the view is open
 * (it owns the "No terminals available" / reconnect states), but a hidden
 * mount would just dial a dead runner, so it stays unmounted.
 */
export function shouldMountTerminalSurface(
  conversationId: string | null,
  terminalFirst:
    | {
        isTerminalFirst: boolean;
        view: "chat" | "terminal";
        terminalsAvailable: boolean;
      }
    | null
    | undefined,
): boolean {
  if (!conversationId || terminalFirst?.isTerminalFirst !== true) return false;
  return terminalFirst.view === "terminal" || terminalFirst.terminalsAvailable;
}

/**
 * One recently-viewed terminal-first session whose terminal surface stays
 * mounted (hidden) after navigating away, so switching back re-reveals a
 * live attach instead of re-dialing. ``readOnly`` is snapshotted from the
 * session's own permission level while it was active — the current
 * session's permissions must never leak onto a warm background surface.
 */
export interface WarmTerminalEntry {
  conversationId: string;
  readOnly: boolean;
}

/**
 * How many sessions' terminal surfaces stay warm at once (the active one
 * included). Each warm surface holds a WebSocket + a runner-side
 * tmux control client, a WebGL context (browsers cap those per page; losing
 * one falls back to xterm's DOM renderer), and keeps parsing any output
 * its TUI streams while hidden — so the cache is bounded rather than
 * unbounded, but sized to cover a working set of sessions, not just a
 * pair. Idle shells cost ~nothing in the background; the practical
 * ceiling is many simultaneously *busy* TUIs.
 */
export const MAX_WARM_TERMINAL_SURFACES = 8;

/**
 * LRU update for the warm terminal-surface cache: move (or insert)
 * *conversationId* at the most-recent end with the given *readOnly*
 * snapshot, evicting the least-recent entry past
 * {@link MAX_WARM_TERMINAL_SURFACES}. Pure — exported for direct unit
 * testing.
 */
export function updateWarmTerminalSurfaces(
  prev: WarmTerminalEntry[],
  conversationId: string,
  readOnly: boolean,
): WarmTerminalEntry[] {
  const rest = prev.filter((e) => e.conversationId !== conversationId);
  return [...rest, { conversationId, readOnly }].slice(-MAX_WARM_TERMINAL_SURFACES);
}

/**
 * The conversation scroll surface + composer — the content of the
 * "Main Agent" tab (and also the standalone view on `/`).
 *
 * In terminal-first sessions, when the header switcher is set to
 * Terminal, the conversation + composer are replaced by an inline
 * `MainTerminalView`. The switcher itself stays visible (in the header,
 * see ViewModeToggle) so the user can flip back to Chat.
 */
function MainAgentSurface({
  conversationId,
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
  selectedAgentId,
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
  showClaudePermissionMode = false,
  showGoalControl = false,
  showClaudeGoalControl = false,
  showPollyCodexGoalControl = false,
  costRoutingEligible,
  subagentRoutingEligible,
  subAgentLabel,
  wrapperLabel,
}: MainAgentSurfaceProps) {
  const terminalFirst = useTerminalFirst();
  // Streaming-hot subscriptions and the bubble pipeline live in <Transcript>.
  // The turn rail is a hover minimap with no mobile affordance (CSS-hidden
  // under `md`). Gate its MOUNT — not just its visibility — on the viewport so
  // mobile never mounts observers and history listeners for a rail it can't see.
  const isMobileViewport = useIsMobileViewport();
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
  // `showTerminal` — Rules of Hooks. The single return at the bottom
  // renders the persistent terminal overlay and, when the terminal view
  // is closed, the chat surface beside it.
  //
  // The bubble pipeline (buildBubbles, the turn rail, elicitation floats,
  // the working indicator) and its streaming-hot store subscriptions live in
  // `<Transcript>` — the only subtree that re-renders per SSE frame. This
  // surface subscribes to nothing hot, so the composer and chrome below bail
  // out of streaming-frame re-renders.

  // Active reply quotes — each "Reply ↵" click appends; consumed by Composer.
  const [replyQuotes, setReplyQuotes] = useState<ReplyQuote[]>([]);
  const nextReplyQuoteId = useRef(0);

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
    setNativeServerSwitcherHidden(serverSwitcherHiddenForSurface(surfaceFrontmost));
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

  // Persistent terminal surfaces for terminal-first sessions. Each is
  // mounted from the moment its terminal is reachable — not just when the
  // view is open — and kept mounted as a visibility-toggled overlay, so
  // the attach (WS dial through the host tunnel + a forked `tmux attach`
  // + full repaint) pre-warms in the background and survives both
  // Chat/Terminal flips AND session switches: a small LRU keeps the last
  // few sessions' surfaces alive after navigating away, so coming back is
  // near-instant instead of re-dialing from scratch. `invisible` (not
  // display:none) keeps a hidden overlay's layout size, so FitAddon
  // geometry stays correct and no resize churn hits tmux; hidden elements
  // don't paint, hit-test, or take focus. The chat surface still unmounts
  // while the terminal is shown — a heavy transcript shouldn't render
  // behind a live terminal.
  const mountTerminal = shouldMountTerminalSurface(conversationId, terminalFirst);
  // Non-owners attach read-only: a shared PTY can't attribute input
  // per-user, so only the owner may type. They drive the agent via the
  // composer instead. Server enforces this too.
  const terminalReadOnly = !isOwnerLevel(permissionLevel);
  const [warmTerminals, setWarmTerminals] = useState<WarmTerminalEntry[]>([]);
  useEffect(() => {
    if (!mountTerminal || !conversationId) return;
    setWarmTerminals((prev) => updateWarmTerminalSurfaces(prev, conversationId, terminalReadOnly));
  }, [mountTerminal, conversationId, terminalReadOnly]);
  // Derive from warmTerminals only — the effect above adds the active session
  // after the first paint, so xterm (a lazy chunk) never loads on the initial
  // render. Previously the active session was included here same-commit to
  // avoid the one-frame delay; that optimization is removed so the terminal
  // chunk defers until after first paint. Returning to an already-warm
  // background session is still same-commit (it's already in warmTerminals).
  const renderedTerminals = warmTerminals;
  const handleTerminalResume = useCallback(async () => {
    if (!conversationId) throw new Error("Session is not available");
    if (liveness.kind === "host_offline" || liveness.kind === "local_stranded") {
      onShowReconnectHelp();
      return;
    }
    const result = await retrySession(conversationId);
    if (!result.recovered) {
      throw new Error("The session is already connected; no recovery was performed");
    }
  }, [conversationId, liveness.kind, onShowReconnectHelp]);
  const terminalSurfaces = renderedTerminals.map((entry) => {
    const isActive = mountTerminal && entry.conversationId === conversationId;
    const isShown = isActive && showTerminal;
    return (
      <div
        key={entry.conversationId}
        className={cn("absolute inset-0 flex flex-col", !isShown && "invisible")}
        aria-hidden={!isShown}
      >
        <MainTerminalView
          conversationId={entry.conversationId}
          initialTerminalKey={isActive ? terminalFirst?.terminalViewKey : null}
          visible={isShown}
          runnerOnline={isActive ? runnerOnline : undefined}
          onResume={isActive ? handleTerminalResume : undefined}
          onSurfaceElement={isActive ? setTerminalSurfaceEl : undefined}
          readOnly={entry.readOnly}
        />
        {isShown && (
          <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
        )}
      </div>
    );
  });

  // Single return so both surfaces keep stable fragment positions across
  // view flips — an early return would change the tree shape and remount
  // the terminal overlays, disposing the live attaches they exist to
  // preserve. The chat surface still unmounts entirely while the
  // terminal is shown (a heavy transcript shouldn't render behind it).
  return (
    <>
      {terminalSurfaces}
      {!showTerminal && (
        <>
          {/* The scrolling transcript column owns every streaming-hot store
          subscription and the bubble pipeline, so an SSE frame re-renders it
          alone — this surface's composer and chrome below bail out. */}
          <Transcript
            setConversationEl={setConversationEl}
            containerEl={containerEl}
            scroller={scroller}
            setScroller={setScroller}
            sendScrollNonce={sendScrollNonce}
            hasMoreHistory={hasMoreHistory}
            loadingMoreHistory={loadingMoreHistory}
            isMobileViewport={isMobileViewport}
            showsWorking={showsWorking}
            agentsError={agentsError}
            sandboxLaunching={sandboxLaunching}
            terminalFirst={terminalFirst}
          />
          {/* Floating reply button — scoped to the conversation container. */}
          <SelectionPopup
            containerRef={conversationRef}
            onReply={(text) =>
              setReplyQuotes((prev) => [
                ...prev,
                { id: `reply-quote-${nextReplyQuoteId.current++}`, text },
              ])
            }
          />

          <Composer
            disabled={disabled}
            status={status}
            isWorking={isWorking}
            onSend={handleSend}
            onSendSlashCommand={handleSendSlashCommand}
            onStop={onStop}
            agents={agents}
            selectedAgentId={selectedAgentId}
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
            showClaudePermissionMode={showClaudePermissionMode}
            showGoalControl={showGoalControl}
            showClaudeGoalControl={showClaudeGoalControl}
            showPollyCodexGoalControl={showPollyCodexGoalControl}
            isTerminalFirst={isTerminalFirst}
            isNativeWrapper={isNativeWrapper}
            unreachable={
              !sandboxLaunching &&
              (liveness.kind === "host_offline" || liveness.kind === "local_stranded")
            }
            onShowReconnectHelp={onShowReconnectHelp}
            costRoutingEligible={costRoutingEligible}
            subagentRoutingEligible={subagentRoutingEligible}
            subAgentLabel={subAgentLabel}
            wrapperLabel={wrapperLabel}
          />

          {/* Reconnect-or-fork banner when unreachable, nothing otherwise.
          Sits below the composer so its position is consistent with the
          terminal view. */}
          <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
        </>
      )}
    </>
  );
}

function HydratingPlaceholder() {
  return (
    <div className="flex flex-1 items-center justify-center gap-2 text-muted-foreground text-ui">
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
        <p className="text-muted-foreground text-ui">
          Couldn't load{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-sm">{conversationId}</code>
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

interface ReplyQuote {
  id: string;
  text: string;
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
  selectedAgentId: string | null;
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
  replyQuotes: ReplyQuote[];
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
  /** Runner-owned model picker rows for native sessions. */
  codexModelOptions: readonly NativeModelOption[];
  /** Show the Codex Plan-mode toggle. */
  showCodexPlanMode: boolean;
  showClaudePermissionMode?: boolean;
  /** Show the session Goal control. */
  showGoalControl?: boolean;
  /** Show Polly's Claude SDK command-backed Goal control. */
  showClaudeGoalControl?: boolean;
  /** Show Polly's Codex command-backed Goal control. */
  showPollyCodexGoalControl?: boolean;
  /**
   * Terminal-first session. Presentation only: tightens the composer's
   * bottom padding to `pb-1.5` (the status line beneath already cushions
   * the edge); non-terminal-first chats use the roomier `pb-3`.
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
   * The session is unreachable (`host_offline` / `local_stranded`): a message
   * can't wake it. The composer is blocked (disabled) and the reconnect
   * banner below is the only affordance.
   */
  unreachable?: boolean;
  /**
   * Open the reconnect help dialog. Always wired to the status line's host
   * badge, which turns itself into a clickable reconnect affordance whenever
   * its bound host is offline and reconnectable.
   */
  onShowReconnectHelp?: () => void;
  /** Session passes `isCostRoutingSession` (polly orchestrator, not a child); see that predicate. */
  costRoutingEligible?: boolean;
  /**
   * Session passes `isSubagentRoutingSession` — a top-level session that gets
   * the gear modal's "Subagent routing" row. Wider than `costRoutingEligible`:
   * native-terminal Claude/Codex sessions qualify here too.
   */
  subagentRoutingEligible?: boolean;
  /**
   * Sub-agent instance label when the active session is a child, e.g.
   * ``"check-account-eligibility"``; ``null``/omitted for top-level
   * sessions. When set, the composer peeks a "Chatting with sub-agent …"
   * tray above the card. See ``subAgentComposerLabel``.
   */
  subAgentLabel?: string | null;
  /**
   * The session's ``omnigent.wrapper`` label, or ``null`` when it carries
   * none. Only the identity label reads it — to name the vendor running a
   * native sub-agent child (see ``composerHarnessLabel``). Behavior gates
   * keep using ``modelPickerKind`` / ``isNativeWrapper``.
   */
  wrapperLabel?: string | null;
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
  skills: readonly { name: string; description: string }[],
  showEffort: boolean,
  showModel: boolean,
  showCompact = true,
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
  skills: readonly { name: string; description: string }[],
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
          <span className="text-sm tabular-nums" aria-hidden="true">
            {usedPct}%
          </span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-44 text-center text-sm">
        <p className="tabular-nums">{usedPct}% of context used.</p>
      </TooltipContent>
    </Tooltip>
  );
}

/**
 * Model label for the composer status tray.
 *
 * @param model - Model override or bound agent model id.
 * @param codexModelOptions - Native model metadata, when available.
 * @returns The advertised display label for known native models, a
 *   version-agnostic friendly form for an alias-shaped id the catalog
 *   doesn't list, the raw model id otherwise, or ``null`` when no model
 *   is known.
 */
export function formatStatusModelLabel(
  model: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const raw = model?.trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const codexOption = findNativeModelOption(codexModelOptions, raw);
  if (codexOption) return codexOption.displayName ?? codexOption.id;
  // An alias-shaped id the session's catalog doesn't list (e.g. during
  // the pre-catalog window): render it friendly mechanically — "sonnet"
  // → "Sonnet", "sonnet_5" → "Sonnet 5", "sonnet[1m]" → "Sonnet
  // (1M context)" — without claiming a version the client can't know.
  // Which model an alias lands on is the harness's answer; the catalog's
  // display name supersedes this wherever one has arrived.
  const alias = /^([a-z]+)(?:_(\d+))?(\[1m\])?$/.exec(lower);
  if (alias) {
    let label = `${alias[1]!.charAt(0).toUpperCase()}${alias[1]!.slice(1)}`;
    if (alias[2]) label += ` ${alias[2]}`;
    if (alias[3]) label += " (1M context)";
    return label;
  }
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
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const codexOption = model ? findNativeModelOption(codexModelOptions, model.trim()) : null;
  const modelLabel = formatStatusModelLabel(model, codexModelOptions);
  const effortLabel = formatStatusEffortLabel(effort, codexOption !== null);
  const parts = [modelLabel, effortLabel].filter((p): p is string => p != null && p.length > 0);
  return parts.length > 0 ? parts.join(" ") : null;
}

/**
 * Identity label for the composer status tray: which harness/agent is
 * running this session. Native vendor wrappers read as the bare vendor
 * name ("Claude" / "Codex"); SDK/bundle agents read as the agent name
 * with the brain harness in parens ("Polly (Pi)"). Lives in the status tray
 * below the composer, separate from the read-only model/effort label.
 *
 * A native sub-agent child (a Claude Code Task, a Codex collab thread) reads
 * as its vendor's product name ("Claude Code"), matching the Agents rail's
 * main row. Its ``sub_agent_name`` is the VENDOR's agent type — Claude's
 * ``subagent_type``, e.g. ``"general-purpose"`` — not an Omnigent agent, so
 * the wrapper label has to win over the name-based path below; the instance
 * itself is already named in the "Chatting with sub-agent …" tray.
 *
 * @param modelPickerKind - Native picker family, when the session is a
 *   claude-/codex-/cursor-native wrapper.
 * @param agentName - Bound agent name (lowercase slug), if any.
 * @param sessionHarness - Effective brain harness id (override-aware).
 * @param harnessLabels - harness id → picker label.
 * @param wrapper - The session's ``omnigent.wrapper`` label, if any.
 * @returns Display label, or ``null`` when nothing is known.
 */
export function composerHarnessLabel(
  modelPickerKind: NativeModelPickerKind | null,
  agentName: string | null | undefined,
  sessionHarness: string | null,
  harnessLabels: Record<string, string> = BRAIN_HARNESS_LABELS,
  wrapper: string | null = null,
): string | null {
  const nativeSubagent = nativeCodingAgentForSubagentWrapper(wrapper);
  if (nativeSubagent) return nativeSubagent.displayName;
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
 * Status tray under the composer: branch left, model/context right.
 * Pulled up behind the card so a shelf peeks below; skips render when empty.
 * Session cost lives in the header agent-info popover, not here.
 */
function ComposerStatusLine({
  goal,
  isSubAgentSession,
  onHostReconnect,
}: {
  goal: Goal | null;
  isSubAgentSession: boolean;
  /**
   * Opens the reconnect help dialog, handed to the host badge — which turns
   * itself into a clickable reconnect affordance when its bound host is
   * offline and reconnectable.
   */
  onHostReconnect?: () => void;
}) {
  const conversationId = useChatStore((s) => s.conversationId);
  const contextWindow = useChatStore((s) => s.contextWindow);
  const tokensUsed = useChatStore((s) => s.tokensUsed);
  const codexPlanMode = useChatStore((s) => s.codexPlanMode);
  // Seeded from the session snapshot on bind (chatStore.sessionBindingPatch),
  // alongside contextWindow — so the branch reads from the same store as
  // the other status-line values rather than a separate fetch.
  const gitBranch = useChatStore((s) => s.gitBranch);

  // Host binding drives whether the HostBadge has anything to show — read it
  // from the same source the badge does so the tray's render guard matches.
  const { session } = useSession(conversationId);
  const isHostBound = !!session?.hostId;

  // PR link → opens the workspace rail's GitHub tab. Shares the info query's
  // cache with the GitHub panel, so opening the tab is instant.
  const github = useGithubInfo(conversationId ?? undefined);
  const openGithubTab = useOpenGithubTab();
  const prNumber = github.data?.pr?.number ?? null;
  const showPr = !!conversationId && !isSubAgentSession && prNumber !== null && !!openGithubTab;

  const showBranch = !!conversationId && !!gitBranch;
  // Host indicator (green/red dot + host name), left of the worktree branch.
  // Hidden on sub-agent sessions — the header's child-session slot owns the
  // back affordance there, mirroring where this badge used to live. HostBadge
  // self-hides when the session isn't host-bound, so also gate on isHostBound
  // (below) before treating the badge as a reason to render the tray.
  const showHost = !!conversationId && !isSubAgentSession;
  const showPlanMode = !!conversationId && codexPlanMode;
  const showGoal = !!conversationId && goal != null;
  // contextWindow > 0: the SSE path validates it but the snapshot path doesn't, and 0/0 → "NaN%".
  const showRing =
    !!conversationId && contextWindow != null && contextWindow > 0 && tokensUsed != null;
  // A host-bound session shows the badge, so the tray must render for it even
  // with no branch/ring yet — otherwise the host + context footer vanishes for
  // sessions with no worktree branch (e.g. codex) until the ring populates.
  // This also keeps the offline host's reconnect affordance on screen, since
  // the badge is where it lives and an unreachable session often has no
  // branch/ring at all.
  const showHostBadge = showHost && isHostBound;
  if (!showBranch && !showPr && !showPlanMode && !showGoal && !showRing && !showHostBadge)
    return null;

  return (
    <div
      data-testid="composer-status-line"
      className={cn(
        // -mt-4 tucks under the card; pt-5.5 keeps content below the overlap.
        "mx-auto -mt-4 flex w-full items-center gap-3 rounded-b-2xl px-4 pb-1.5 pt-5.5",
        CHAT_COLUMN_WIDTH,
      )}
    >
      {/* Left: host + branch. flex-1 keeps the right cluster pinned; truncate, no wrap. */}
      <div className="flex min-w-0 flex-1 items-center gap-3 text-sm text-muted-foreground">
        {showHost && conversationId && (
          <HostBadge sessionId={conversationId} onReconnect={onHostReconnect} />
        )}
        {showBranch && (
          <span className="flex min-w-0 items-center gap-1.5">
            <GitBranchIcon className="ui-icon" />
            <span data-testid="composer-git-branch" className="min-w-0 truncate" title={gitBranch}>
              {gitBranch}
            </span>
          </span>
        )}
        {showPr && (
          <button
            type="button"
            data-testid="composer-pr-link"
            onClick={() => openGithubTab?.()}
            title="View this PR in the GitHub tab"
            className="flex shrink-0 items-center gap-1.5 rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          >
            <GithubMono size={14} aria-hidden />
            <span className="tabular-nums underline underline-offset-2">#{prNumber}</span>
          </button>
        )}
      </div>
      {/* Right: model/effort and context ring, never shrinks. */}
      <div className="flex min-w-0 shrink-0 items-center gap-3">
        {showPlanMode && (
          <span
            data-testid="composer-plan-mode"
            className="inline-flex items-center gap-1 text-sm font-medium text-foreground"
          >
            <FileTextIcon className="ui-icon" />
            <span>Plan mode</span>
          </span>
        )}
        {showGoal && goal && <GoalStatusPill goal={goal} />}
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
        "mx-auto -mb-4 flex w-full items-center gap-1.5 rounded-t-2xl bg-brand-accent/10 px-4 pt-1.5 pb-5.5 text-sm text-brand-accent",
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
 * Pill above the composer tallying running background tasks (a dev server, a
 * background shell, a sub-agent), shown independently of the "Working…" shimmer.
 *
 * The tally expands into a card listing each running shell by name: on hover
 * for a mouse, on tap for touch (which focuses it, so a tap outside closes it
 * via blur), and on focus for the keyboard. It morphs in place — the same
 * element tweens width and height to the measured content size at a constant
 * corner radius (CSS can't animate to an `auto` size), growing upward out of an
 * absolute layer over a hidden spacer so the composer below never shifts. A
 * count-only edge (older runner, no per-shell detail) stays a plain tally.
 *
 * The whole pill floats as an overlay pinned just above the composer (its form
 * is `relative`, this is `bottom-full`) rather than taking a flow row — a
 * reserved row would butt against the transcript's bottom overflow edge and
 * clip the last line. Only the pill itself takes pointer events so the
 * transcript underneath stays interactive.
 */
export function BackgroundTaskPill() {
  const bgCount = useChatStore((s) => s.backgroundTaskCount);
  const bgTasks = useChatStore((s) => s.backgroundTasks);
  const [open, setOpen] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<{ width: number; height: number } | null>(null);
  const canExpand = bgTasks.length > 0;

  useEffect(() => {
    if (bgCount <= 0 || !canExpand) setOpen(false);
  }, [bgCount, canExpand]);

  // Measure the current content so the container tweens to a concrete size in
  // both dimensions. Re-measured whenever the state or the task list changes.
  useLayoutEffect(() => {
    const el = contentRef.current;
    if (el) setBox({ width: el.offsetWidth, height: el.offsetHeight });
  }, [open, canExpand, bgCount, bgTasks]);

  if (bgCount <= 0) return null;

  const showCard = open && canExpand;

  return (
    // Floats above the composer instead of taking a flow row: a reserved row
    // would butt against the transcript's bottom overflow edge and clip its
    // last line. Pinned to the composer's top (bottom-full) and re-applying the
    // form's px-4/md:px-6 inset so the pill lines up with the composer card.
    // pointer-events-none lets the transcript underneath stay scrollable /
    // selectable — only the pill itself re-enables them.
    <div className="pointer-events-none absolute inset-x-0 bottom-full px-4 md:px-6">
      <div className={cn("mx-auto flex w-full px-1 pb-1.5", CHAT_COLUMN_WIDTH)}>
        <div className="pointer-events-auto relative">
          {/* Reserves the collapsed footprint so the absolute, upward-growing
            card never shoves the composer. */}
          <div aria-hidden className="invisible px-3 py-1.5 text-sm">
            <div className="flex items-center gap-1.5 whitespace-nowrap">
              <SquareTerminalIcon className="size-3.5 shrink-0" aria-hidden="true" />
              <span>
                {bgCount} background task{bgCount === 1 ? "" : "s"}
              </span>
            </div>
          </div>
          <div
            role="status"
            data-testid="background-task-pill"
            aria-label={`${bgCount} background task${bgCount === 1 ? "" : "s"} still running`}
            tabIndex={canExpand ? 0 : undefined}
            // Hover opens ONLY for a real mouse. On touch, opening on emulated
            // hover triggers iOS's "first tap reveals hover, second tap clicks"
            // heuristic — which swallows the first tap inconsistently. Touch opens
            // via onClick instead, so the first tap always lands.
            onPointerEnter={(e) => {
              if (e.pointerType === "mouse" && canExpand) setOpen(true);
            }}
            onPointerLeave={(e) => {
              if (e.pointerType === "mouse") setOpen(false);
            }}
            // Tap/click: open and focus the pill so onBlur can close it on
            // tap-out (a gesture-driven focus() works even on iOS, where tapping a
            // non-button element otherwise won't focus it).
            onClick={(e) => {
              if (!canExpand) return;
              setOpen(true);
              e.currentTarget.focus({ preventScroll: true });
            }}
            onFocus={() => canExpand && setOpen(true)}
            onBlur={(e) => {
              // Close when focus leaves the pill entirely (tap/click outside,
              // Tab away); staying open if it moves to a child.
              if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
            }}
            style={box ? { width: box.width, height: box.height } : undefined}
            className={cn(
              "absolute bottom-0 left-0 overflow-hidden rounded-2xl bg-card text-sm shadow-sm ring-1 ring-border transition-[width,height,box-shadow] duration-200 ease-out",
              showCard && "z-10 shadow-menu",
            )}
          >
            <div
              ref={contentRef}
              // Cap at Tailwind's `md` container (28rem/448px), but never wider
              // than the viewport minus a margin so it can't overflow on a narrow
              // screen. The margin exceeds the composer's own px-4/px-6 inset (this
              // pill sits at px-1) so the card's right edge, ring included, stays
              // inside the composer rather than overhanging it.
              style={
                showCard ? { maxWidth: "min(var(--container-md, 28rem), 100vw - 3rem)" } : undefined
              }
              className={cn("w-max text-left", showCard ? "px-3 py-2" : "px-3 py-1.5")}
            >
              {showCard ? (
                <ul className="flex animate-in flex-col gap-1.5 fade-in-0 duration-200">
                  {bgTasks.map((task, i) => {
                    const label = task.description || task.command || "Background shell";
                    const cmd = task.command && task.command !== label ? task.command : null;
                    return (
                      <li key={task.id ?? i} className="flex items-start gap-2">
                        <SquareTerminalIcon
                          className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
                          aria-hidden="true"
                        />
                        <div className="min-w-0">
                          <div className="truncate text-foreground">{label}</div>
                          {cmd ? (
                            <div className="truncate font-mono text-xs text-muted-foreground">
                              {cmd}
                            </div>
                          ) : null}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <div className="flex items-center gap-1.5 whitespace-nowrap text-muted-foreground">
                  <SquareTerminalIcon className="size-3.5 shrink-0" aria-hidden="true" />
                  <span>
                    {bgCount} background task{bgCount === 1 ? "" : "s"}
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
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
  selectedAgentId,
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
  showClaudePermissionMode = false,
  showGoalControl = false,
  showClaudeGoalControl = false,
  showPollyCodexGoalControl = false,
  isTerminalFirst = false,
  isNativeWrapper = false,
  unreachable = false,
  onShowReconnectHelp,
  costRoutingEligible = false,
  subagentRoutingEligible = false,
  subAgentLabel = null,
  wrapperLabel = null,
}: ComposerProps) {
  const [value, setValue] = useState("");
  const [submitWithModEnter] = useState(() => readSubmitWithModEnter());
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
  // Text + attachments handed back by a send that failed before the server
  // took ownership. Drained below so the message can be retried.
  const failedSendDraft = useChatStore((s) => s.failedSendDraft);
  // The conversation whose draft the composer's value/files currently hold.
  // Trails `conversationId` by one commit across a session switch; see the
  // draft-restore effect.
  const [settledConversationId, setSettledConversationId] = useState<string | null>(null);
  // Nonce bumped when bare "/model" is submitted; opens the AgentPicker
  // dropdown instead of sending (see submit()).
  const [pickerOpenNonce, setPickerOpenNonce] = useState(0);
  // Single send-telemetry point (see submit()). Emitting here rather than via
  // the Button's componentId covers Enter-key sends too — a textarea Enter never
  // submits the form, so it would otherwise bypass the Button entirely.
  const { trackClick } = useOmnigentAnalytics();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Declared after textareaRef so dictation can place the caret after the
  // text it inserts (and insert at the caret rather than the draft's end).
  const dictation = useDictationInsert(value, setValue, textareaRef);
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

  const codexPlanMode = useChatStore((s) => s.codexPlanMode);
  // Harness/agent identity shown in the status tray below the card, separate
  // from the composer's read-only model/effort label.
  const sessionHarness = useChatStore((s) => s.sessionHarness);
  const subAgentName = useChatStore((s) => s.subAgentName);
  const brainHarnessLabels = useBrainHarnessLabels();
  const harnessLabel = composerHarnessLabel(
    modelPickerKind,
    // For a sub-agent (head) session, identify the head family being viewed
    // (e.g. the GPT head → "Gpt") rather than the bundle orchestrator
    // ("Debby") — the bundle is already named in the breadcrumb / Agents rail.
    // A native sub-agent's name is vendor-side, not an Omnigent head, so the
    // wrapper below outranks it.
    subAgentName ??
      agents?.find((a) => a.id === selectedAgentId)?.name ??
      agents?.[0]?.name ??
      null,
    sessionHarness,
    brainHarnessLabels,
    wrapperLabel,
  );

  // Preserve unsent text + file attachments per session so switching
  // tabs and coming back restores the draft. The shared draft store also lets
  // the sidebar surface which sessions have unfinished composer content.
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
  const { goal, setGoal: setGoalState } = useGoalState(conversationId, showGoalControl);
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
  // Composer text captured when voice dictation starts, so Esc can revert to it.
  const voiceSnapshotRef = useRef("");
  // On mobile, programmatic focus immediately summons the software keyboard.
  // Keep desktop's fast-type affordance, but let mobile users explicitly tap
  // the composer when switching back from Terminal or changing sessions.
  const isMobile = useIsMobileViewport();
  const isCoarsePointer = useIsCoarsePointer();
  const preventsKeyboardSubmit = isMobile || isCoarsePointer;
  const isMobileRef = useRef(isMobile);
  isMobileRef.current = isMobile;

  useEffect(() => {
    const restored = conversationId ? getSessionDraft(conversationId) : undefined;
    setValue(restored?.text ?? "");
    setFiles(restored?.files ?? []);
    dirtyRef.current = false;
    // Publish which conversation the composer's text now belongs to. The
    // failed-send restore below reads value/files through refs, which still
    // hold the OUTGOING conversation's text during this commit — it waits for
    // this to settle rather than mistaking that for "the user is typing".
    setSettledConversationId(conversationId ?? null);
    if (!isMobileRef.current) textareaRef.current?.focus();

    return () => {
      if (!conversationId || !dirtyRef.current) return;
      setSessionDraft(conversationId, {
        text: valueRef.current,
        files: filesRef.current,
      });
    };
  }, [conversationId]);

  // Publish edits as they happen so the open sidebar updates immediately,
  // rather than only learning about a draft when this composer unmounts.
  useEffect(() => {
    if (!conversationId || settledConversationId !== conversationId || !dirtyRef.current) return;
    setSessionDraft(conversationId, { text: value, files });
  }, [conversationId, settledConversationId, value, files]);

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
  const menuMatches = menuOpen ? rankedSlashCommandNames(slashCommands, menuQuery) : [];

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

  // Restore the text (and attachments) of a send that failed, so the user can
  // fix and resend instead of retyping. The composer is empty in the normal
  // case — `submit` clears it optimistically — so only fill it when the user
  // hasn't already started something new; their in-progress text wins. Files
  // are re-validated on the way in: when the upload itself was what failed
  // (a 415 on an unsupported type), re-arming the same file would only fail
  // again, so it's dropped with the same inline reason a fresh attach gives.
  useEffect(() => {
    if (failedSendDraft === null) return;
    if (failedSendDraft.conversationId !== conversationId) return;
    // Wait for the draft-restore effect to settle this conversation's text
    // into value/files. Reading the refs mid-switch would see the PREVIOUS
    // conversation's draft and wrongly conclude the user is mid-sentence,
    // dropping the failed message on the way back to the session it failed in.
    if (settledConversationId !== conversationId) return;
    useChatStore.setState({ failedSendDraft: null });
    // The user started something new while the send was in flight — their
    // in-progress text wins over a clobbering restore.
    if (valueRef.current.trim() !== "" || filesRef.current.length > 0) return;
    setValue(failedSendDraft.text);
    dirtyRef.current = true;
    if (failedSendDraft.files.length > 0) {
      const { accepted, errors } = validateAttachments(failedSendDraft.files);
      setFiles(accepted);
      setAttachmentError(errors.length > 0 ? errors.join("\n") : null);
    }
    if (!isMobileRef.current) textareaRef.current?.focus();
  }, [failedSendDraft, conversationId, settledConversationId]);

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
        // Native reported-model sessions additionally show the pending
        // indicator until the harness's own report settles the ask.
        const harness = useChatStore.getState().sessionHarness;
        void useChatStore
          .getState()
          .setModel(clear ? null : target, {
            expectConfirmation: harness === "claude-native" || harness === "codex-native",
          })
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
  // Growth stays in the flex column so the transcript viewport ends where the
  // composer begins instead of letting the card cover visible output.
  useAutoGrowTextarea(textareaRef, value, 10);

  // Scope recall to the active conversation so ArrowUp surfaces only this
  // chat's prompts, not the last thing typed in any other chat.
  const { appendEntry, recallPrevious, recallNext, resetCursor } = usePromptHistory(conversationId);
  // Set just before recall sets `value`; cleared when the resulting onChange
  // fires. Lets onChange distinguish "user typed" (reset cursor) from
  // "recall replaced the value" (keep cursor).
  const recallingRef = useRef(false);

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

  // Files dropped anywhere in the chat column attach here, not just on the
  // composer box. Scoped to the column so the sidebar and workspace rail keep
  // their own drag behavior; with no such ancestor the card is the target.
  const [dropTarget, setDropTarget] = useState<HTMLElement | null>(null);
  const bindComposerCard = useCallback((el: HTMLDivElement | null) => {
    setDropTarget(el?.closest<HTMLElement>("[data-chat-surface]") ?? el);
  }, []);
  const isDragActive = useFileDropTarget(dropTarget, addFiles);

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

    // A send is actually happening: report it for both pointer clicks (which
    // reach here via the form submit) and Enter-key sends. Placed after the
    // guard so guarded no-ops don't emit, matching the disabled Send button.
    trackClick("chat.composer.send", "button");

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
      // Bare "/model" when the session has a switchable model (claude-native):
      // sent as plaintext it would open Claude's interactive selector inside the
      // vendor TUI, which the web UI can't render — the session just blocks. Open
      // the composer's config gear modal (which owns the Model dropdown) instead
      // and let the user choose there. "/model <name>" takes the builtin route
      // below to setModel — the same write the modal makes.
      //
      // Runner-backed catalogs (now including claude-native) can arrive after
      // bind. Until rows exist, fall through to the builtin "/model" handler,
      // which surfaces the current model as a read-only hint instead of opening
      // a model-less modal. ("/model <name>" still routes to setModel there.)
      const canOpenModelPicker = codexModelOptions.length > 0;
      if (cmd === "/model" && !arg && showModels && canOpenModelPicker) {
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
            .map((quote) =>
              quote.text
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

    // Touch-primary newline behavior outranks autocomplete and desktop submit
    // preferences. Leave the event untouched so the textarea inserts it.
    if (preventsKeyboardSubmit && e.key === "Enter") {
      return;
    }

    const shouldSubmitFromKeyboard = isComposerSendKey(
      {
        key: e.key,
        shiftKey: e.shiftKey,
        metaKey: e.metaKey,
        ctrlKey: e.ctrlKey,
        altKey: e.altKey,
        isComposing: e.nativeEvent.isComposing,
      },
      submitWithModEnter,
      preventsKeyboardSubmit,
    );
    // Plain Enter still completes an open suggestion. In Mod+Enter mode, the
    // explicit send chord bypasses suggestions so the modifier has one meaning.
    const shouldPreferSendOverCompletion = submitWithModEnter && shouldSubmitFromKeyboard;

    // "@"-mention menu navigation (shared useMentionBrowser) — mutually
    // exclusive with the slash menu below (a mention token can't also read as a
    // "/"-command). Takes priority over history recall and submission.
    if (!shouldPreferSendOverCompletion && handleMentionKeyDown(e)) return;

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
      if (
        !shouldPreferSendOverCompletion &&
        (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !isMobile)) &&
        menuIndex >= 0
      ) {
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

    // Mobile Enter behavior takes precedence over this desktop preference:
    // software-keyboard Enter inserts a newline and Send remains an explicit tap.
    if (shouldSubmitFromKeyboard) {
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
        "chat-composer-form relative px-4 md:px-6",
        isTerminalFirst ? "pb-1.5" : "pb-3",
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
      {/* Background tasks that outlive the turn show as a pill, not the
          "Working…" shimmer. Floats as an overlay above the composer (the form
          is `relative`) rather than a flow row, so it never clips the
          transcript's last line. Self-gates to null otherwise. */}
      <BackgroundTaskPill />
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
      {/* Drop cue, spanning the chat column this composer belongs to. */}
      {isDragActive && dropTarget ? <FileDropOverlay container={dropTarget} /> : null}
      {/* Single rounded container — textarea + action row. No focus-within
          ring; drag-over still lifts an inset ring. dark:bg-card-solid so
          upper trays (queued / sub-agent) don't ghost through glass --card. */}
      <div
        ref={bindComposerCard}
        // Opaque card edge for transcript clearance; status shelf below is translucent.
        data-composer-card
        className={cn(
          "relative mx-auto flex w-full flex-col rounded-2xl border border-border bg-card dark:bg-card-solid shadow-composer transition-[border-color,box-shadow] has-[textarea:focus]:shadow-composer-focus",
          CHAT_COLUMN_WIDTH,
          isDragActive && "ring-2 ring-ring ring-inset",
        )}
      >
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
              <div key={quote.id} className="flex items-start gap-2">
                <div className="min-w-0 flex-1 bg-muted/40 rounded-md border-l-2 border-l-primary/60 px-2 py-1.5 text-sm text-muted-foreground">
                  <span className="block truncate">
                    {quote.text.length > 120 ? `${quote.text.slice(0, 120)}…` : quote.text}
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
        <div className="relative overflow-hidden">
          {composerIsCommand && (
            <div
              ref={backdropRef}
              aria-hidden
              data-testid="composer-highlight-overlay"
              className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-4 pt-3 pb-2 text-ui text-foreground"
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
              // A rejected attachment is never added, so there's no chip to
              // remove and nothing else would ever clear this. Left sticky it
              // reads as a blocker on a composer the user can actually submit.
              if (attachmentError !== null) setAttachmentError(null);
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
            onFocus={() => {
              // From here the textarea's caret is one the user placed, so
              // dictation inserts there instead of at the end of the draft.
              dictation.noteFocus();
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
                          : "Send a message…"
            }
            rows={1}
            disabled={disabled || isReadOnly || unreachable || hasPendingElicitation}
            data-slash-command={composerIsCommand ? "true" : undefined}
            className={cn(
              "relative w-full resize-none overflow-y-auto bg-transparent px-4 pt-3 pb-2 text-ui outline-none [scrollbar-width:none] placeholder:text-muted-foreground disabled:opacity-60 [&::-webkit-scrollbar]:hidden",
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
                key={attachmentKey(file)}
                className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
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
          <div className="px-4 pb-2 text-sm text-destructive whitespace-pre-wrap">
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
                className="flex items-center gap-1 rounded-full border border-border bg-muted px-2 py-0.5 text-sm text-muted-foreground"
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
          <div className="px-4 pb-2 text-sm text-muted-foreground whitespace-pre-wrap">
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
              componentId="chat.composer.attach_files"
            >
              <PaperclipIcon className="size-4" data-icon-size="16" />
              <span className="sr-only">Attach files</span>
            </Button>
            <ComposerMicButton
              enableHotkey
              disabled={disabled || isReadOnly || hasPendingElicitation}
              onVoiceStart={() => {
                voiceSnapshotRef.current = value;
              }}
              onVoiceDiscard={() => {
                setValue(voiceSnapshotRef.current);
              }}
              onTranscript={(text) => {
                dictation.appendFinal(text);
                dirtyRef.current = true;
                // Dictation is a user-driven edit — exit prompt-recall mode
                // so ArrowUp/ArrowDown don't clobber the dictated text.
                resetCursor();
                if (commandError !== null) setCommandError(null);
              }}
              onInterim={(text) => {
                dictation.replaceInterim(text);
                dirtyRef.current = true;
                resetCursor();
              }}
            />
          </div>
          {/* Right side: read-only model/effort label + config gear + Send.
              Smart Routing lives inside the gear modal — folded into the Model
              dropdown for Claude, a standalone Switch for other routable
              agents. */}
          <div className="flex min-w-0 items-center gap-0.5">
            {showCodexPlanMode && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    size="sm"
                    variant={codexPlanMode ? "secondary" : "ghost"}
                    className={cn(
                      "h-9 gap-1.5 px-2 text-sm md:h-8",
                      codexPlanMode && "border border-ring/30 text-foreground",
                    )}
                    disabled={isReadOnly || planModeBusy}
                    aria-pressed={codexPlanMode}
                    aria-label={codexPlanMode ? "Exit Plan mode" : "Enter Plan mode"}
                    data-testid="codex-plan-mode-toggle"
                    data-active={codexPlanMode ? "true" : undefined}
                    onClick={() => void toggleCodexPlanMode()}
                    componentId="chat.composer.toggle_plan_mode"
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
            {showGoalControl && (
              <GoalControl
                conversationId={conversationId}
                readOnly={isReadOnly}
                goal={goal}
                onGoalChange={setGoalState}
                backendLabel="Codex"
              />
            )}
            {showClaudeGoalControl && (
              <GoalControl
                mode="command"
                conversationId={conversationId}
                readOnly={isReadOnly}
                onStartGoal={(condition) => onSend(`/goal ${condition}`)}
                backendLabel="Claude"
              />
            )}
            {showPollyCodexGoalControl && (
              <GoalControl
                mode="command"
                conversationId={conversationId}
                readOnly={isReadOnly}
                onStartGoal={(condition) => onSend(`/goal ${condition}`)}
                backendLabel="Codex"
              />
            )}
            <div className="flex min-h-9 min-w-0 items-center rounded-lg transition-colors empty:hidden md:min-h-8 has-[button:not([aria-disabled=true])]:hover:bg-muted dark:has-[button:not([aria-disabled=true])]:hover:bg-muted/50 [&>button]:bg-transparent!">
              <ComposerModelEffortLabel
                showModels={showModels}
                showEffort={showEffort}
                modelPickerKind={modelPickerKind}
                codexModelOptions={codexModelOptions}
                costRoutingEligible={costRoutingEligible}
                harnessLabel={harnessLabel}
              />
              <ComposerConfigGear
                harnessLabel={harnessLabel}
                showModels={showModels}
                showEffort={showEffort}
                showClaudePermissionMode={showClaudePermissionMode}
                effortLevels={effortLevels}
                modelPickerKind={modelPickerKind}
                codexModelOptions={codexModelOptions}
                costRoutingEligible={costRoutingEligible}
                subagentRoutingEligible={subagentRoutingEligible}
                // Config changes persist server-side and apply on the next
                // wake/turn (the runner forward is best-effort), so the gear
                // stays live wherever a message could be sent — including
                // asleep/starting/unknown. Only read-only viewers and sessions
                // no message can wake (unreachable) get an inert gear.
                disabled={isReadOnly || unreachable}
                openNonce={pickerOpenNonce}
              />
            </div>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="submit"
                    size="icon"
                    variant={showInterruptButton ? "destructive" : "default"}
                    // Send button fades more decisively when there's no draft —
                    // overrides the base 50% disabled-opacity so the affordance
                    // reads as "waiting for input", not "almost active".
                    className={cn(
                      "size-9 shrink-0 rounded-lg md:size-8",
                      !showInterruptButton &&
                        "hover:bg-primary/90 disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100",
                    )}
                    // Interrupt stays live during a pending elicitation —
                    // cancelling the turn is the other legitimate way out.
                    disabled={
                      showInterruptButton
                        ? isReadOnly
                        : !hasDraft || disabled || isReadOnly || hasPendingElicitation
                    }
                    title={showInterruptButton ? "Interrupt" : undefined}
                    aria-label={showInterruptButton ? "Interrupt" : "Send"}
                  >
                    {showInterruptButton ? (
                      <SquareIcon className="size-4 fill-current" />
                    ) : (
                      <ArrowUpIcon className="size-4" viewBox="4 4 16 16" />
                    )}
                    <span className="sr-only">{showInterruptButton ? "Interrupt" : "Send"}</span>
                  </Button>
                </TooltipTrigger>
                {!showInterruptButton && !preventsKeyboardSubmit && (
                  <KeyboardShortcutTooltipContent
                    label="Send"
                    keys={composerSendShortcutKeys(submitWithModEnter)}
                  />
                )}
              </Tooltip>
            </TooltipProvider>
          </div>
        </div>
      </div>
      <ComposerStatusLine
        goal={goal}
        isSubAgentSession={subAgentLabel != null}
        onHostReconnect={onShowReconnectHelp}
      />
    </form>
  );
}

/**
 * Whether the main chat's display-only "Working…" indicator should light up.
 *
 * @param sessionStatus - The main session status, e.g. ``"running"``.
 * @param options - Display gates for the main chat indicator.
 * @param options.hasPendingElicitation - ``true`` when an elicitation prompt
 *   owns the in-progress slot and should suppress the shimmer/pinned pill.
 * @param options.runnerOnline - Runner liveness: ``true`` online, ``false``
 *   known offline, ``undefined`` before the health poll resolves. A known-offline
 *   runner suppresses the indicator ONLY when the session is otherwise idle: a
 *   session actively reporting ``running``/``waiting`` cannot have an offline
 *   runner, so its live status wins over the ``/health`` poll — which polls at a
 *   10s cadence and reads stale-offline during the runner's connect window on a
 *   fresh session's first turn (it would otherwise hide "Working…" for seconds).
 * @param options.backgroundTaskCount - Background shells still running after
 *   the turn ended. A claude-native turn settles to ``idle`` (the status file's
 *   edge) even while shells run, so the bare status alone would hide the
 *   indicator; a positive count keeps it lit so "N background tasks still running"
 *   stays visible.
 * @param options.localSendInFlight - This client's own send is in flight
 *   (``chatStore.status === "streaming"``). Lights the indicator optimistically
 *   the moment the user presses Enter, before any server edge confirms the turn
 *   — the sidebar row already does this (see ``isStartingUp`` in Sidebar.tsx),
 *   so without it the two disagree for the dispatch round-trip. Distinct from
 *   ``sessionStatus``, which mirrors the server: this one means "we asked", not
 *   "the agent is working".
 * @returns ``true`` when the main session's own status should render Working.
 */
export function computeShowsWorking(
  sessionStatus: SessionStatus,
  options: {
    hasPendingElicitation: boolean;
    runnerOnline: boolean | undefined;
    backgroundTaskCount?: number;
    localSendInFlight?: boolean;
  },
): boolean {
  if (options.hasPendingElicitation) return false;
  const isWorking = computeIsWorking(sessionStatus);
  // A running/waiting session is proof the runner is up, so a stale
  // poll-derived ``runnerOnline === false`` must not suppress it. Only gate on
  // known-offline for the not-actively-working case (e.g. a background-shell
  // tally on an idle session). An in-flight local send is the same kind of
  // proof — the user just dispatched — so it also survives the gate.
  if (options.runnerOnline === false && !isWorking && !options.localSendInFlight) return false;
  return isWorking || options.localSendInFlight === true || (options.backgroundTaskCount ?? 0) > 0;
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

// Import sources whose resume reconstructs conversation context from the
// omnigent-stored transcript, so it carries onto any chosen host. Kimi has no
// resume path (blank context regardless of host); kiro/qwen resume only from a
// local recording file that exists on the original machine, so a different host
// starts blank. The in-app resume picker is restricted to this portable set.
const HOST_PORTABLE_IMPORT_SOURCES = new Set(["claude", "codex", "pi", "opencode"]);

/**
 * Whether an unbound session may be resumed in-app via the "Resume on a machine"
 * picker. The picker calls `launch_runner`, which requires the caller to OWN the
 * session, and — for imported sessions — only reconstructs context for
 * host-portable harnesses. Non-owners (who would 404) and kimi/kiro/qwen imports
 * (which would launch with no context) are excluded so they route to the
 * terminal reconnect path instead of a picker that fails or starts blank.
 *
 * @param unbound - Session has no host and no runner.
 * @param isOwner - Caller holds owner level on the session.
 * @param importSource - `omnigent.import.source` label, or null for non-imports
 *   (e.g. an unbound fork, which is host-portable and owned by its creator).
 */
export function unboundSessionResumableInApp(params: {
  unbound: boolean;
  isOwner: boolean;
  importSource: string | null | undefined;
}): boolean {
  if (!params.unbound || !params.isOwner) return false;
  return params.importSource == null || HOST_PORTABLE_IMPORT_SOURCES.has(params.importSource);
}

const EFFORT_LEVELS = ["low", "medium", "high"] as const;

/** Anthropic-side efforts for claude-native sessions (matches ANTHROPIC_EFFORTS in reasoning_effort.py). */
const CLAUDE_NATIVE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

/** Pi thinking ladder (matches PI_EFFORTS in reasoning_effort.py; ``ultra`` aliases to ``max`` on Pi so omitted). */
const PI_NATIVE_EFFORT_LEVELS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

type NativeModelPickerKind = "claude" | "codex" | "cursor" | "kiro" | "opencode" | "pi";

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

/**
 * A custom (label-less) session resolved to the native Codex harness.
 *
 * Custom YAML agents get no `omnigent.wrapper` presentation label, so the
 * resolved harness is the capability evidence. Any wrapper label — including
 * sub-agent variants like `codex-native-ui-subagent`, which cannot honor
 * mid-session overrides — keeps the label authoritative and skips the
 * fallback.
 */
function isLabelLessCodexNative(
  conv:
    { labels?: Record<string, string | null> | null; harness?: string | null } | null | undefined,
): boolean {
  return conv?.labels?.["omnigent.wrapper"] == null && conv?.harness === "codex-native";
}

export function effortLevelsForConv(
  conv:
    { labels?: Record<string, string | null> | null; harness?: string | null } | null | undefined,
  codexModelOptions: readonly NativeModelOption[] = [],
  currentModel: string | null = null,
): readonly string[] {
  switch (conv?.labels?.["omnigent.wrapper"]) {
    case "claude-code-native-ui":
      return CLAUDE_NATIVE_EFFORT_LEVELS;
    case "codex-native-ui":
      return codexEffortLevelsForModel(codexModelOptions, currentModel);
    case "pi-native-ui":
      return PI_NATIVE_EFFORT_LEVELS;
    default:
      return isLabelLessCodexNative(conv)
        ? codexEffortLevelsForModel(codexModelOptions, currentModel)
        : EFFORT_LEVELS;
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
  conv:
    { labels?: Record<string, string | null> | null; harness?: string | null } | null | undefined,
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
    case "pi-native-ui":
      // Like cursor: the runner types a model switch into the live Pi process
      // (via the bridge inbox → Pi's ``setModel``) and Pi mirrors its own
      // ``/model`` picks back to ``model_override`` via the extension's
      // model_select handler, so the picker surfaces that as the live model.
      return "pi";
    default:
      return isLabelLessCodexNative(conv) ? "codex" : null;
  }
}

export function shouldShowModelPicker(
  conv:
    { labels?: Record<string, string | null> | null; harness?: string | null } | null | undefined,
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
  conv:
    { labels?: Record<string, string | null> | null; harness?: string | null } | null | undefined,
): boolean {
  return supportsEffortControl(conv);
}

export function shouldShowCodexPlanModeControl(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return isCodexNativeSession(conv);
}

/**
 * True when the claude-native permission-mode picker should be visible.
 *
 * Claude-native sessions only: the switch drives Claude Code's own
 * shift+tab cycle, which no other harness has.
 *
 * :param conv: Session-like object carrying `labels`; a missing session
 *     or missing labels fails closed.
 * :returns: True only for sessions running the claude-native wrapper.
 */
export function shouldShowClaudePermissionModeControl(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return isClaudeNativeSession(conv);
}

/**
 * True when the session Goal control should be visible.
 *
 * @param conv - Session or sidebar row carrying labels. ``null`` or missing
 *   labels fail closed.
 * @returns True only for Codex-native wrapper sessions until the server
 *   advertises a generic goal capability.
 */
export function shouldShowGoalControl(
  conv: { labels?: Record<string, string | null> | null } | null | undefined,
): boolean {
  return isCodexNativeSession(conv);
}

/** True for top-level Polly sessions running on the Claude SDK harness. */
export function shouldShowPollyClaudeGoalControl(
  session: Pick<Session, "agentName" | "harness" | "parentSessionId"> | null | undefined,
): boolean {
  return (
    session?.parentSessionId == null &&
    session?.agentName?.toLowerCase() === "polly" &&
    session?.harness === "claude-sdk"
  );
}

/** True for top-level Polly sessions running on the Codex harness. */
export function shouldShowPollyCodexGoalControl(
  session: Pick<Session, "agentName" | "harness" | "parentSessionId"> | null | undefined,
): boolean {
  return (
    session?.parentSessionId == null &&
    session?.agentName?.toLowerCase() === "polly" &&
    session?.harness === "codex"
  );
}

/** Title-case an effort level for the status label (``"high"`` → ``"High"``). */
function formatEffortLabel(effort: string): string {
  return effort.charAt(0).toUpperCase() + effort.slice(1);
}

/** Gear-modal row governing the routing of sub-agents the session spawns. */
const SUBAGENT_ROUTING_LABEL = "Subagent routing";
const SUBAGENT_ROUTING_DESCRIPTION = "Model routing for subagents this session spawns";

/**
 * In-session run-config modal opened from the composer's gear icon. The
 * live-committing analogue of the new-session ``HarnessConfigModal``: only the
 * knobs switchable mid-session appear — Model (which folds Smart Routing in as
 * an option where a dropdown exists), Effort, and Subagent routing. A session's
 * own Smart Routing is otherwise a create-time choice, and
 * permission/approval/cursor modes are launch-time only (no in-session state to
 * read or write), so they are intentionally absent.
 *
 * Like the new-session modal, changes are drafted locally and only applied on
 * Save (through the store setters ``setModel`` / ``setEffort`` /
 * ``setCostControlMode`` / ``setSubagentRouting``); Cancel / dismiss discards
 * them. The setters enforce the model↔routing mutual exclusion server-side.
 */
function SessionConfigModal({
  open,
  onOpenChange,
  harnessLabel,
  showModels,
  showEffort,
  showClaudePermissionMode = false,
  effortLevels,
  modelPickerKind,
  codexModelOptions,
  costRoutingEligible,
  subagentRoutingEligible,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  harnessLabel: string | null;
  showModels: boolean;
  showEffort: boolean;
  showClaudePermissionMode?: boolean;
  effortLevels: readonly string[];
  modelPickerKind: NativeModelPickerKind | null;
  codexModelOptions: readonly NativeModelOption[];
  costRoutingEligible: boolean;
  subagentRoutingEligible: boolean;
}) {
  const selectedEffort = useSessionEffort();
  const claudePermissionMode = useChatStore((s) => s.claudePermissionMode);
  const costControlModeOverride = useChatStore((s) => s.costControlModeOverride);
  const subagentRoutingOverride = useChatStore((s) => s.subagentRoutingOverride);
  const conversationId = useChatStore((s) => s.conversationId);
  const { llmModel, usesServerModelOptions, modelOptions, pickerSelectedModel, modelLabel } =
    useResolvedComposerModel(modelPickerKind, codexModelOptions);

  // Agents with a Model dropdown fold Smart Routing into it as an option. There
  // is no in-session switch for agents without one — they choose at create.
  const liveRoutingOn = costRoutingEligible && costControlModeOverride === "on";

  // Resolve the row the current model maps to: the explicit override wins,
  // else the bound `llmModel` implicitly selects its row (so a launch-resolved
  // model shows without an explicit pick), else the catalog default. Falls back
  // to the "Default" sentinel when nothing resolves.
  const implicitModelId =
    pickerSelectedModel === null && usesServerModelOptions
      ? (findNativeModelOption(codexModelOptions, llmModel)?.id ??
        codexModelOptions.find((option) => option.isDefault)?.id ??
        null)
      : null;
  const resolvedModelId = pickerSelectedModel ?? implicitModelId;

  // Local draft — seeded from the live state each time the modal opens so
  // Cancel discards and re-opening always reflects the committed config.
  // `draftModelId` is the resolved model id (null = Default sentinel);
  // `draftRoutingOn` folds Smart Routing in as a mutually-exclusive choice.
  const [draftModelId, setDraftModelId] = useState<string | null>(resolvedModelId);
  const [draftEffort, setDraftEffort] = useState<string | null>(selectedEffort);
  const [draftRoutingOn, setDraftRoutingOn] = useState(liveRoutingOn);
  const [draftPermissionMode, setDraftPermissionMode] = useState(claudePermissionMode);
  // The sub-agent row is stored as a PICK, not a pre-seeded draft:
  // `undefined` means "untouched", so the row mirrors the live stored value for
  // as long as the user hasn't chosen anything. A draft seeded once per open
  // would keep showing the value the session had when the modal opened — and
  // Save would write that stale value back — if the stored one changed
  // underneath (a bind/refresh landing, or another client's PATCH).
  const [pickedSubagentRouting, setPickedSubagentRouting] = useState<"on" | "off" | undefined>(
    undefined,
  );
  useEffect(() => {
    if (!open) return;
    setDraftModelId(resolvedModelId);
    setDraftEffort(selectedEffort);
    setDraftRoutingOn(liveRoutingOn);
    setDraftPermissionMode(claudePermissionMode);
    setPickedSubagentRouting(undefined);
    // Nothing pushes a routing-switch change to the client (no SSE event, and
    // the session query never goes stale), so re-read them here — otherwise the
    // switches show whatever they were at bind time.
    void useChatStore.getState().refreshSessionOverrides();
    // Seed once per open from the current live values, and re-seed if the bound
    // session changes under an open modal (its drafts describe the old one).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, conversationId]);

  // The Select value: the router sentinel when routing is drafted on, else the
  // drafted model, else the "Default" sentinel (no override).
  const modelValue = draftRoutingOn ? MODEL_SELECT_SMART : (draftModelId ?? MODEL_SELECT_DEFAULT);

  // Codex advertises a per-model effort ladder, so the dropdown must follow the
  // DRAFTED model, not the committed one the parent computed — else picking
  // Luna still lists Sol's "ultra". "Default" (null) resolves to the catalog
  // default; non-codex harnesses use a model-independent ladder, so keep the
  // parent's list.
  const draftEffortLevels = useMemo(() => {
    if (modelPickerKind !== "codex") return effortLevels;
    const modelForEffort =
      draftModelId ?? codexModelOptions.find((o) => o.isDefault)?.id ?? llmModel;
    const levels = codexEffortLevelsForModel(codexModelOptions, modelForEffort);
    return levels.length > 0 ? levels : effortLevels;
  }, [modelPickerKind, codexModelOptions, draftModelId, llmModel, effortLevels]);

  // Drop a picked level the newly-drafted model doesn't offer (Sol's "ultra"
  // when switching to Luna) so no stale rung shows and Save never submits a
  // level the model rejects.
  const clampDraftEffortToModel = (modelId: string | null) => {
    if (modelPickerKind !== "codex") return;
    setDraftEffort((prev) => {
      if (prev === null) return null;
      const modelForEffort = modelId ?? codexModelOptions.find((o) => o.isDefault)?.id ?? llmModel;
      return codexEffortLevelsForModel(codexModelOptions, modelForEffort).includes(prev)
        ? prev
        : null;
    });
  };

  const onModelChange = (value: string) => {
    if (value === MODEL_SELECT_SMART) {
      setDraftRoutingOn(true);
      setDraftModelId(null);
      // The router picks the model (and its effort) per turn, so an explicit
      // effort can't apply — reset it so it doesn't ride along frozen.
      setDraftEffort(null);
    } else if (value === MODEL_SELECT_DEFAULT) {
      setDraftModelId(null);
      setDraftRoutingOn(false);
      clampDraftEffortToModel(null);
    } else {
      // Pinning a model turns routing off (mutually exclusive).
      setDraftModelId(value);
      setDraftRoutingOn(false);
      clampDraftEffortToModel(value);
    }
  };

  // Two-state row. A row stored before the switch became explicit carries no
  // value, and that reads as Default — the same thing "off" means — so the
  // trigger never shows a third state. Untouched (`undefined`) reads through
  // to the stored value, so what the row shows is always what is persisted.
  const effectiveSubagentRouting =
    pickedSubagentRouting === undefined ? subagentRoutingOverride : pickedSubagentRouting;
  const subagentRoutingValue = effectiveSubagentRouting === "on" ? "on" : "off";

  const save = () => {
    // Commit the changed knobs SEQUENTIALLY, awaiting each PATCH before the
    // next. Claude-native applies model/effort changes by typing separate
    // ``/model``/``/effort`` slash commands into its terminal, so firing them
    // concurrently lets the injections interleave into one line (e.g.
    // ``/model opus/effort medium``, which Claude rejects as a model name).
    // Awaiting serializes them — each change lands in its own turn. Order
    // matches HarnessConfigModal.save: model/routing first (their setters
    // enforce the mutual exclusion server-side), then effort.
    void (async () => {
      const store = useChatStore.getState();
      try {
        if (draftRoutingOn) {
          if (costRoutingEligible && !liveRoutingOn) await store.setCostControlMode("on");
        } else {
          // Re-pin the model when routing was on and there's a Model dropdown
          // (its setter cleared the applied override, and `resolvedModelId`
          // reflects the leftover cross-session sticky — not what's applied — so
          // the ``!==`` guard would false-negative), or whenever the drafted
          // model actually changed. For a no-dropdown agent (e.g. Polly) the
          // user can't have chosen a model, so re-pinning the seeded
          // `resolvedModelId` would risk pinning a leaked sticky — turning
          // routing off just clears via setModel(null) below.
          const modelChanged = draftModelId !== resolvedModelId;
          const rePinAfterRouting = liveRoutingOn && showModels;
          if (rePinAfterRouting || modelChanged)
            await store.setModel(draftModelId, {
              // Reported-model sessions confirm through the harness's own
              // report — mark the ask pending until it (or the not-applied
              // error) settles.
              expectConfirmation: modelPickerKind === "claude" || modelPickerKind === "codex",
            });
          if (costRoutingEligible && liveRoutingOn) await store.setCostControlMode("off");
        }
        // Skip effort while routing is on: the router picks it per turn, and a
        // stray ``/effort`` injection would just be noise.
        if (showEffort && !draftRoutingOn && draftEffort !== selectedEffort)
          await store.setEffort(draftEffort);
        // Same pane as /model + /effort, so this must stay in the awaited
        // sequence rather than firing concurrently.
        if (showClaudePermissionMode && draftPermissionMode !== claudePermissionMode)
          await store.setClaudePermissionMode(draftPermissionMode);
        // Sub-agent routing is independent of this session's own model — a
        // plain PATCH with no slash-command injection, so ordering is free.
        // Only an explicit pick is written, and only when it still differs from
        // what the session has now: an untouched row must never persist a value
        // the user didn't choose, and an unset store value already means "off",
        // so picking Default on such a session writes nothing.
        if (
          subagentRoutingEligible &&
          pickedSubagentRouting !== undefined &&
          pickedSubagentRouting !== (store.subagentRoutingOverride ?? "off")
        )
          await store.setSubagentRouting(pickedSubagentRouting);
      } catch {
        // Individual setters already roll back their optimistic state; a failed
        // PATCH shouldn't wedge the modal open.
      }
    })();
    onOpenChange(false);
  };

  const modelSelectOptions = useMemo(() => {
    const catalog = usesServerModelOptions
      ? modelOptions.map((m) => ({ id: m.id, label: nativeModelLabel(m) }))
      : modelOptions.map((m) => ({ id: m.id, label: m.label ?? m.id }));
    // Smart Routing pins the router's fully-qualified pick
    // (``databricks-claude-opus-4-8``), which the harness catalog carries only
    // under an alias (``opus``) — or not at all. Radix falls back to the
    // placeholder for a value no item declares, so the row rendered BLANK on a
    // routed session. Carry the live model as its own option: the trigger then
    // names the model the session is actually on, matching the status label
    // below the composer. Nothing is submitted for an untouched row — `save`
    // re-pins only a changed draft.
    if (draftModelId === null || catalog.some((option) => option.id === draftModelId)) {
      return catalog;
    }
    return [...catalog, { id: draftModelId, label: modelLabel ?? draftModelId }];
  }, [usesServerModelOptions, modelOptions, draftModelId, modelLabel]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="composer-config-modal">
        <DialogHeader>
          <DialogTitle>Configure {harnessLabel ?? "session"}</DialogTitle>
          <DialogDescription className="sr-only">
            Change how this session runs. Model, effort, and smart routing apply to the next turn.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-5 py-1">
          {showModels && (
            <ConfigRow label="Model" description="Underlying LLM">
              <RoutingModelSelect
                value={modelValue}
                onValueChange={onModelChange}
                offerSmartRouting={costRoutingEligible}
                testId="composer-config-model"
                models={modelSelectOptions}
                // Claude's and Codex's Default resolves to a model the
                // spellings don't make obvious, so name it here exactly as
                // the landing dialog does — one shared labeling.
                defaultLabel={
                  modelPickerKind === "codex" || modelPickerKind === "claude"
                    ? defaultModelLabel(modelOptions)
                    : undefined
                }
                activeModelId={draftModelId}
                componentId="chat.composer.model"
              />
            </ConfigRow>
          )}
          {showEffort && (
            <ConfigRow label="Effort" description="Reasoning depth vs. speed">
              <Select
                // Routing picks the model (and its effort) per turn, so an
                // explicit effort is meaningless: the row is frozen and reads as
                // an em-dash placeholder (Radix shows it for the empty value).
                value={draftRoutingOn ? "" : (draftEffort ?? EFFORT_SELECT_NONE)}
                onValueChange={(v) => setDraftEffort(v === EFFORT_SELECT_NONE ? null : v)}
                disabled={draftRoutingOn}
                componentId="chat.composer.effort"
                valueHasNoPii
              >
                <SelectTrigger
                  className="w-full"
                  data-testid="composer-config-effort"
                  aria-label="Effort"
                >
                  <SelectValue placeholder={EFFORT_UNAVAILABLE_PLACEHOLDER} />
                </SelectTrigger>
                <SelectContent
                  position="popper"
                  align="start"
                  className="w-(--radix-select-trigger-width)"
                >
                  <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
                  {draftEffortLevels.map((level) => (
                    <SelectItem
                      key={level}
                      value={level}
                      data-effort-level={level}
                      // Codex efforts render raw (its ids aren't title-cased);
                      // other harnesses title-case for display.
                      className={modelPickerKind === "codex" ? undefined : "capitalize"}
                    >
                      {formatStatusEffortLabel(level, modelPickerKind === "codex") ?? level}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </ConfigRow>
          )}
          {/* Hidden when the mode is unknown — Claude only renders its mode
              footer in some pane states, and a guess would misreport it. */}
          {showClaudePermissionMode && claudePermissionMode !== "" && (
            <ConfigRow label="Permissions" description="How much Claude asks before acting">
              <Select
                value={draftPermissionMode}
                onValueChange={setDraftPermissionMode}
                componentId="chat.composer.permission_mode"
                valueHasNoPii
              >
                <SelectTrigger
                  className="w-full"
                  data-testid="composer-config-permission-mode"
                  aria-label="Permission mode"
                >
                  <SelectValue>{claudePermissionModeLabel(draftPermissionMode)}</SelectValue>
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  {CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES.map((mode) => (
                    <SelectItem
                      key={mode.value}
                      value={mode.value}
                      data-permission-mode={mode.value}
                    >
                      <span className="flex flex-col items-start">
                        <span>{mode.label}</span>
                        <span className="text-muted-foreground text-xs">{mode.description}</span>
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </ConfigRow>
          )}
          {/* Sub-agent routing — the only in-session routing control, for a
          native session whose launch installed the spawn-routing apparatus
          (spawns route through the harness hook) and for SDK/bundle agents
          (spawns route through the create path). Hidden where it would be
          inert: pinned Codex, and any plain native session. A session's own
          routing is a create-time choice. Two options — a session that started
          on Smart Routing was stamped "on" at create, so an unset value is
          Default and the trigger always shows what's stored. */}
          {subagentRoutingEligible && (
            <ConfigRow label={SUBAGENT_ROUTING_LABEL} description={SUBAGENT_ROUTING_DESCRIPTION}>
              <Select
                value={subagentRoutingValue}
                onValueChange={(v) => setPickedSubagentRouting(v === "on" ? "on" : "off")}
                componentId="chat.composer.subagent_routing"
                valueHasNoPii
              >
                <SelectTrigger
                  className="w-full"
                  data-testid="composer-config-subagent-routing"
                  aria-label={SUBAGENT_ROUTING_LABEL}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent position="popper" align="start">
                  <SelectItem value="on" data-subagent-routing="on">
                    {SMART_ROUTING_LABEL}
                  </SelectItem>
                  <SelectItem value="off" data-subagent-routing="off">
                    Default
                  </SelectItem>
                </SelectContent>
              </Select>
            </ConfigRow>
          )}
        </div>

        <DialogFooter className="border-t-0 bg-transparent">
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            data-testid="composer-config-cancel"
          >
            Cancel
          </Button>
          <Button type="button" onClick={save} data-testid="composer-config-save">
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Composer gear affordance: a ghost `SettingsIcon` that shows the session's
 * live run-config on hover and opens `SessionConfigModal` on click. Rendered
 * only when the session has at least one switchable knob (model, effort, or
 * smart routing) — otherwise there's nothing to configure.
 *
 * @param openNonce External "open the modal" signal, nonce-keyed so repeat
 *   requests re-open (bare ``/model`` submits route here now that the composer
 *   trigger is a read-only label). ``0`` / omitted means never requested.
 */
function ComposerConfigGear({
  harnessLabel,
  showModels,
  showEffort,
  showClaudePermissionMode = false,
  effortLevels,
  modelPickerKind,
  codexModelOptions,
  costRoutingEligible,
  subagentRoutingEligible,
  disabled,
  openNonce = 0,
}: {
  harnessLabel: string | null;
  showModels: boolean;
  showEffort: boolean;
  showClaudePermissionMode?: boolean;
  effortLevels: readonly string[];
  modelPickerKind: NativeModelPickerKind | null;
  codexModelOptions: readonly NativeModelOption[];
  costRoutingEligible: boolean;
  subagentRoutingEligible: boolean;
  disabled: boolean;
  openNonce?: number;
}) {
  const [open, setOpen] = useState(false);
  const appliedOpenNonce = useRef(0);
  useEffect(() => {
    if (!openNonce || openNonce === appliedOpenNonce.current) return;
    // Consume the nonce even when disabled so a later enable doesn't replay a
    // stale open request; skip opening while the gear is inert (read-only /
    // unreachable), matching the click guard.
    appliedOpenNonce.current = openNonce;
    if (disabled) return;
    setOpen(true);
  }, [openNonce, disabled]);
  const summary = useSessionConfigSummary({
    harnessLabel,
    showModels,
    showEffort,
    modelPickerKind,
    codexModelOptions,
    costRoutingEligible,
  });

  if (
    !showModels &&
    !showEffort &&
    !costRoutingEligible &&
    !subagentRoutingEligible &&
    !showClaudePermissionMode
  )
    return null;

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              size="icon"
              variant="ghost"
              // Soft-disable: keep the button hover-able so its tooltip (the
              // read-only config summary) still shows, but block the click and
              // dim it. A native `disabled` button swallows pointer events, so
              // the tooltip would never fire.
              // The ::before hairline is this gear's divider from the
              // model/effort label in the composer's split pill. It's drawn
              // here rather than as a sibling node because the label renders
              // nothing for some sessions — `first:` then drops the divider.
              className={cn(
                "size-9 shrink-0 text-muted-foreground before:absolute before:top-1/2 before:left-0 before:h-4 before:w-px before:-translate-y-1/2 before:bg-border hover:text-foreground first:before:hidden md:size-8",
                disabled && "cursor-default opacity-50 hover:text-muted-foreground",
              )}
              aria-disabled={disabled}
              onClick={() => {
                if (disabled) return;
                setOpen(true);
              }}
              data-testid="composer-config-gear"
              aria-label="Configure session"
            >
              <SettingsIcon className="size-4" data-icon-size="16" />
            </Button>
          </TooltipTrigger>
          {summary.length > 0 && (
            <TooltipContent
              side="top"
              className="flex-col items-start gap-0.5 px-3 py-2"
              data-testid="composer-config-gear-tooltip"
            >
              {summary.map((row) => (
                <span key={row.label} className="text-muted-foreground">
                  {row.label}:{" "}
                  <span className="text-background dark:text-popover-foreground">{row.value}</span>
                </span>
              ))}
            </TooltipContent>
          )}
        </Tooltip>
      </TooltipProvider>
      <SessionConfigModal
        open={open}
        onOpenChange={setOpen}
        harnessLabel={harnessLabel}
        showModels={showModels}
        showEffort={showEffort}
        showClaudePermissionMode={showClaudePermissionMode}
        effortLevels={effortLevels}
        modelPickerKind={modelPickerKind}
        codexModelOptions={codexModelOptions}
        costRoutingEligible={costRoutingEligible}
        subagentRoutingEligible={subagentRoutingEligible}
      />
    </>
  );
}

/**
 * Label/value rows summarizing the session's live run-config, for the gear
 * icon's hover tooltip. Mirrors the new-session summary but with in-session
 * values. The Permissions row lives in the modal itself, not this summary.
 */
function useSessionConfigSummary({
  harnessLabel,
  showModels,
  showEffort,
  modelPickerKind,
  codexModelOptions,
  costRoutingEligible,
}: {
  harnessLabel: string | null;
  showModels: boolean;
  showEffort: boolean;
  modelPickerKind: NativeModelPickerKind | null;
  codexModelOptions: readonly NativeModelOption[];
  costRoutingEligible: boolean;
}): { label: string; value: string }[] {
  const selectedEffort = useSessionEffort();
  const costControlModeOverride = useChatStore((s) => s.costControlModeOverride);
  const { modelLabel } = useResolvedComposerModel(modelPickerKind, codexModelOptions);
  const routingOn = costRoutingEligible && costControlModeOverride === "on";

  const rows: { label: string; value: string }[] = [];
  if (harnessLabel) rows.push({ label: "Harness", value: harnessLabel });
  if (showModels) {
    rows.push({
      label: "Model",
      value: routingOn ? SMART_ROUTING_LABEL : (modelLabel ?? "Default"),
    });
  }
  // Suppress Effort while routing is on: the router picks the model and its
  // effort per turn, so a pinned effort doesn't apply and would mislead.
  if (showEffort && !routingOn) {
    const effortValue = formatStatusEffortLabel(selectedEffort, modelPickerKind === "codex");
    rows.push({ label: "Effort", value: effortValue ?? "Default" });
  }
  return rows;
}

/**
 * The effort this conversation is actually at.
 *
 * `sessionReasoningEffort` is conversation-scoped, so two live conversations at
 * different efforts each read their own; `selectedEffort` is the single
 * app-global sticky pick, used only as the pre-hydration fallback. Reading the
 * sticky pick alone would show a warm-switched conversation the last effort
 * picked anywhere.
 */
function useSessionEffort(): string | null {
  const sessionReasoningEffort = useChatStore((s) => s.sessionReasoningEffort);
  const stickyEffort = useChatStore((s) => s.selectedEffort);
  return sessionReasoningEffort ?? stickyEffort;
}

/**
 * Resolve the session's live model + its picker option list from the store,
 * per harness family. Shared by the AgentPicker trigger, the gear config
 * modal, and the gear hover summary so all three agree on what "the current
 * model" is (the resolution differs by wrapper — see the inline notes).
 *
 * @param modelPickerKind Native picker family, or ``null`` for SDK/bundle.
 * @param codexModelOptions Server-provided model options (codex/cursor/…).
 */
function useResolvedComposerModel(
  modelPickerKind: NativeModelPickerKind | null,
  codexModelOptions: readonly NativeModelOption[],
) {
  const sessionModelOverride = useChatStore((s) => s.sessionModelOverride);
  const llmModel = useChatStore((s) => s.llmModel);
  const nativeVendorOwnsModel = useChatStore((s) => s.nativeVendorOwnsModel);

  // Native model pickers populate from the snapshot's runner-backed
  // ``model_options`` field. Claude's rows are the aliases pinned to the
  // launch-time Databricks catalog; Codex carries richer effort metadata.
  const usesServerModelOptions =
    modelPickerKind === "claude" ||
    modelPickerKind === "codex" ||
    modelPickerKind === "cursor" ||
    modelPickerKind === "kiro" ||
    modelPickerKind === "pi" ||
    modelPickerKind === "opencode";
  const modelOptions: readonly {
    id: string;
    label?: string;
    displayName?: string;
    isDefault?: boolean;
  }[] = usesServerModelOptions ? codexModelOptions : [];
  const isNativeModelPicker = modelPickerKind !== null;

  // The harness's own report is the display authority for claude-/codex-
  // native sessions: `llmModel` carries the verbatim reported model (the
  // launch's own report, or an in-pane switch), and the chip, the gear
  // highlight, and the hover summary all resolve from it alone. The user's
  // request (`sessionModelOverride`) and the cross-session sticky
  // (`selectedModel`) are inputs, never display state — rendering a request
  // as if it were truth is exactly how a record/pane divergence hides.
  const isReportedModelPicker = modelPickerKind === "claude" || modelPickerKind === "codex";
  // The row the reported model maps to: its catalog row when one matches
  // exactly (by id or wire model), else the raw reported value itself — the
  // modal appends that as its own honest row rather than relabeling it onto
  // a same-family row of a different generation.
  const reportedRowId =
    isReportedModelPicker && llmModel
      ? (findNativeModelOption(codexModelOptions, llmModel)?.id ?? llmModel)
      : null;
  // Before the harness has reported anything (a routed session whose pane
  // has not started, a reload of one), the gear seeds from the session's
  // own REQUEST so the pick the user (or the router) made is what the row
  // offers to edit — a request is never display truth, but it is the draft.
  // The moment a report exists it wins, so a request the pane never took
  // can't masquerade as the active model.
  const requestedRowId =
    isReportedModelPicker && sessionModelOverride
      ? (findNativeModelOption(codexModelOptions, sessionModelOverride)?.id ?? sessionModelOverride)
      : null;
  // cursor mirrors its live TUI model into ``model_override``; kiro sets it
  // on a web pick (which also drives a live ``/model`` switch); opencode/pi
  // mirror both ways into ``model_override``. Those wrappers keep their
  // override-derived surface until they adopt reported-model semantics.
  // SDK/bundle agents (no native picker) resolve the session override or the
  // bound default — never the cross-session sticky.
  const pickerSelectedModel = isReportedModelPicker
    ? (reportedRowId ?? requestedRowId)
    : sessionModelOverride;
  const effectiveModel = nativeVendorOwnsModel
    ? modelPickerKind === "cursor" || modelPickerKind === "kiro"
      ? sessionModelOverride
      : modelPickerKind === "opencode" || modelPickerKind === "pi"
        ? (sessionModelOverride ?? llmModel)
        : null
    : isReportedModelPicker
      ? llmModel
      : (sessionModelOverride ?? llmModel);
  const modelLabel = formatStatusModelLabel(effectiveModel, codexModelOptions);
  return {
    llmModel,
    usesServerModelOptions,
    modelOptions,
    isNativeModelPicker,
    pickerSelectedModel,
    effectiveModel,
    modelLabel,
  };
}

/**
 * Read-only ``<Model> <Effort>`` label in the composer, left of the config
 * gear. Model switching / effort control now live in the gear modal, so this
 * is a glanceable status label rather than a dropdown trigger — model in the
 * foreground, effort muted. Renders nothing when neither is known/switchable
 * (the harness identity and full config live in the gear tooltip/modal).
 *
 * @param showModels Whether the session exposes a model to surface.
 * @param showEffort Whether the session exposes a reasoning-effort level.
 * @param modelPickerKind Native picker family, or ``null`` for SDK/bundle.
 * @param codexModelOptions Server-provided model options (codex/cursor/…).
 * @param costRoutingEligible Whether Smart Routing is offered for this session.
 * @param harnessLabel Harness identity (e.g. "Polly (Pi)"), used as the label
 *   fallback for SDK/bundle agents that surface no model/effort.
 */
function ComposerModelEffortLabel({
  showModels,
  showEffort,
  modelPickerKind,
  codexModelOptions,
  costRoutingEligible,
  harnessLabel,
}: {
  showModels: boolean;
  showEffort: boolean;
  modelPickerKind: NativeModelPickerKind | null;
  codexModelOptions: readonly NativeModelOption[];
  costRoutingEligible: boolean;
  harnessLabel: string | null;
}) {
  const selectedEffort = useSessionEffort();
  const costControlModeOverride = useChatStore((s) => s.costControlModeOverride);
  const pendingModelChange = useChatStore((s) => s.pendingModelChange);
  const { modelLabel } = useResolvedComposerModel(modelPickerKind, codexModelOptions);
  const routingOn = costRoutingEligible && costControlModeOverride === "on";
  // An asked-but-unconfirmed switch on a reported-model session: the chip
  // keeps the harness's model; this spinner is the honest "asked, not yet
  // true" marker until the report (or the not-applied error) settles it.
  const modelPending =
    pendingModelChange !== null && (modelPickerKind === "claude" || modelPickerKind === "codex") ? (
      <Loader2Icon
        data-testid="composer-model-pending"
        aria-label="Model change pending"
        className="ml-1 inline size-3 shrink-0 animate-spin text-muted-foreground"
      />
    ) : null;
  // Routing picks the model + effort per turn, so the label reads
  // "Smart Routing" with no pinned model/effort — matching the tooltip.
  if (routingOn) {
    return (
      <span
        data-testid="composer-model-effort-label"
        className="min-w-0 shrink truncate pl-2.5 pr-2 text-sm tabular-nums text-muted-foreground"
      >
        <span className="text-foreground">{SMART_ROUTING_LABEL}</span>
      </span>
    );
  }
  const effortLabel =
    showEffort && selectedEffort
      ? formatStatusEffortLabel(selectedEffort, modelPickerKind === "codex")
      : null;
  // SDK/bundle sessions (no native picker) still surface their resolved model
  // in the label even though the gear modal has no Model dropdown for them —
  // showModels gates only the modal control, not this read-out.
  const model = showModels || modelPickerKind === null ? modelLabel : null;
  // SDK/bundle agents (e.g. Polly) that resolve no model/effort fall back to
  // the harness identity ("Polly (Pi)") so the slot isn't empty. Scoped to
  // SDK/bundle (modelPickerKind === null): native wrappers keep an empty label
  // when their model is unresolved rather than surfacing the bare vendor name,
  // which the gear tooltip already shows.
  if (!model && !effortLabel) {
    if (modelPickerKind !== null || !harnessLabel) return null;
    return (
      <span
        data-testid="composer-model-effort-label"
        className="min-w-0 shrink truncate pl-2.5 pr-2 text-sm tabular-nums text-muted-foreground"
      >
        <span className="text-foreground">{harnessLabel}</span>
      </span>
    );
  }

  return (
    <span
      data-testid="composer-model-effort-label"
      className="min-w-0 shrink truncate pl-2.5 pr-2 text-sm tabular-nums text-muted-foreground"
    >
      {model && <span className="text-foreground">{model}</span>}
      {model && effortLabel && " "}
      {effortLabel && <span className="text-muted-foreground">{effortLabel}</span>}
      {modelPending}
    </span>
  );
}
