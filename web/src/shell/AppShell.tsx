import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Outlet, useParams, useSearchParams } from "@/lib/routing";
import {
  PROJECT_LABEL_KEY,
  type Conversation,
  useConversations,
  useProjects,
} from "@/hooks/useConversations";
import { conversationDisplayLabel, UNTITLED_CONVERSATION_LABEL } from "./sidebarNav";
import { useSessionAgent } from "@/hooks/useAgents";
import { useApproveHotkey } from "@/hooks/useApproveHotkey";
import { useSidebarToggleHotkeys } from "@/hooks/useSidebarToggleHotkeys";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useNewSessionHotkey } from "@/hooks/useNewSessionHotkey";
import { useIsEmbedded } from "@/lib/embedded";
import { AgentInfoContent, agentHasInfo } from "@/components/AgentInfo";
import { useIdleNotifications } from "@/hooks/useIdleNotifications";
import { useSeedReadState } from "@/hooks/useUnseenConversations";
import { useIOSViewportLock } from "@/hooks/useIOSViewportLock";
import { readFilesPanelPreferences, writeFilesPanelPreferences } from "@/lib/filesPanelPreferences";
import { derivePermissionLevel, isOwnerLevel } from "@/lib/permissionsApi";
import {
  isAndroidShell,
  isIOSShell,
  isMacElectronShell,
  onNativeSidebarDrag,
  supportsBrowser,
  updateBridge,
} from "@/lib/nativeBridge";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import {
  buildDesignModePrompt,
  dataUrlToFile,
  type DesignModeElement,
} from "@/lib/designModePrompt";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import {
  readDefaultWorkspacePanelOpen,
  writeDefaultWorkspacePanelOpen,
} from "@/lib/workspacePanelPreferences";
import { readTranscriptViewDefault } from "@/lib/transcriptViewPreferences";
import { updateOverlayToastOffset } from "@/lib/updateOverlayInset";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  cachedTreeContains,
  executionLogTabKey,
  MAIN_EXECUTION_LOG_KEY,
  MAX_TREE_DEPTH,
  useChildSessions,
} from "@/hooks/useChildSessions";
import { useDebugMode } from "@/hooks/useDebugMode";
import { useBrowserAgentRelay } from "@/hooks/useBrowserAgentRelay";
import { resyncBrowserSuppression } from "@/hooks/useSuppressBrowserView";
import {
  findAgentTerminal,
  inventoryTerminals,
  isAgentTerminalKey,
  PANEL_NO_TERMINAL_KEY,
  terminalTabKey,
  useDeleteTerminal,
  useTerminals,
} from "@/hooks/useTerminals";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import {
  isNativeWrapper as isNativeWrapperLabel,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { isCurrentServerLocal } from "@/lib/serverOrigin";
import { useChatStore } from "@/store/chatStore";
import {
  STARTING_GRACE_S,
  livenessRowFromSession,
  useSessionLiveness,
} from "@/hooks/useSessionLiveness";
import { useResizableInlinePanel } from "@/hooks/useResizableInlinePanel";
import { useResizableSidebar } from "@/hooks/useResizableSidebar";
import { ChatHeader } from "./ChatHeader";
import { ExecutionLogsPanel } from "./ExecutionLogsPanel";
import { FileViewer } from "./FileViewer";
import { FileViewerContext } from "./FileViewerContext";
import { FilesPanelDrawer } from "./FilesPanelDrawer";
import type { ChangedSort } from "./FlatFileList";
import { MobilePanelDrawer } from "./MobilePanelDrawer";
import { isMobileViewport, Sidebar } from "./Sidebar";
import { SidebarHeaderActions } from "./SidebarHeaderActions";
import { useSettingsRoute } from "./settingsNav";
import { SubagentsPanel } from "./SubagentsPanel";
import { useRootSessionId, useSession } from "@/hooks/useSession";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";
import { TerminalsPanel } from "./TerminalsPanel";
import { PermissionsModal } from "@/components/PermissionsModal";
import { KeyboardShortcutsDialog } from "@/components/KeyboardShortcutsDialog";
import { CommandPalette } from "./CommandPalette";
import { Toaster } from "@/components/ui/sonner";
import { CloseShellDialog } from "./CloseShellDialog";
import { ForkSessionDialog } from "./ForkSessionDialog";
import { ForkDialogContextProvider, type ForkDialogContextValue } from "./ForkDialogContext";
import { InlineTerminalsSection } from "./InlineTerminalsSection";
import { WorkspacePanel } from "./WorkspacePanel";
import { SessionRail } from "./SessionRail";
import type { RightRailTab } from "./railTabs";

/**
 * Top-level layout. The sidebar and right panels are responsive:
 *
 *   - **Mobile (`< md`)**: fixed full-screen overlays. When open they cover
 *     the chat with a translate-x slide-in. The sidebar's own X button
 *     dismisses (no backdrop — the overlay covers the viewport edge-to-edge,
 *     so there is no "outside" to click, and a `bg-black/20` layer behind
 *     it caused a persistent grey artifact at the iOS safe-area insets).
 *   - **Desktop (`md+`)**: static flex siblings. Open ↔ closed animates
 *     each panel's width, pushing the main content accordingly. No backdrop —
 *     side panels aren't covering anything.
 *
 * The right slot holds either `FilesPanel` (file tree / changed list) or
 * `FileViewer` (code + comments) — never both at once. Selecting a file
 * transitions from the list to the code view in the same slot. The "← Back"
 * button in `FileViewer` returns to the list.
 *
 * Default open state is taken from the initial viewport: the left sidebar is
 * open on desktop and closed on mobile. The right files panel starts closed.
 *
 * **Mobile session-rail entry**: the desktop right column has no room on
 * a phone, so the rail's contents are reached via a top-right FAB that
 * opens a dropdown with "Files", "Changes" and "Terminals" (the latter
 * only when one or more terminals exist). Each entry opens the matching
 * push panel — the FAB and the desktop rail tabs route through the same
 * open*() handlers.
 *
 * **Right rail tabs (desktop)**: the aside is internally tabbed between
 * Files, Changes, Terminals and Agents so each can claim the full rail
 * height instead of competing for a vertically-split slot. Files (full
 * folder tree) and Changes (changed-files-only flat list) are peer tabs —
 * the selected tab *is* the scope. Opening a file (chat link or rail click)
 * forces the rail to a files scope so the viewer is visible. Terminal-first
 * sessions render the terminal inline in main. Shells have no nav tab — they
 * open as closable soft tabs in the rail's tab strip. The Agents tab only
 * appears once there's more than one agent (the root has at least one child).
 */

// `null` in panel state means Chat view, but an absent sessionStorage entry
// means "use the configured default." Keep an explicit marker so choosing Chat
// can override a Terminal default when the user returns to that conversation.
const CHAT_VIEW_STORAGE_VALUE = "__chat__";

/**
 * Resolve the terminal-view target for `?view=terminal`.
 *
 * The URL param names only the SURFACE (chat vs terminal), never which
 * terminal, so restoring straight from it would rewrite a rail-opened shell
 * to the agent pane. The precise target lives in the per-session store, which
 * the panel setter writes alongside the param — prefer it, and fall back to
 * the agent terminal for a deep link that carries no stored target.
 *
 * :param stored: Per-session stored panel key (null when absent).
 * :param agentKey: The session's agent-terminal key (or the no-target sentinel).
 * :returns: The terminal key the view should open on.
 */
function resolveTerminalViewKey(stored: string | null, agentKey: string): string {
  return stored !== null && stored !== CHAT_VIEW_STORAGE_VALUE ? stored : agentKey;
}

export function AppShell() {
  // Cmd/Ctrl+Enter accepts the pending harness approval prompt. Bound once
  // here so it works on every chat route, regardless of where focus sits.
  useApproveHotkey();

  // Lock the iOS shell to the visual viewport so the soft keyboard can't pan
  // the whole document (which would hide the header and break the layout).
  // No-op off the iOS shell. Scoped here so auth pages keep normal scrolling.
  useIOSViewportLock();

  const desktopUpdates = useMemo(() => updateBridge(), []);
  const [updateOverlayHeight, setUpdateOverlayHeight] = useState(0);
  useEffect(() => {
    if (!desktopUpdates?.getOverlayHeight || !desktopUpdates.onOverlayHeight) return;
    let alive = true;
    let receivedPush = false;
    const unsubscribe = desktopUpdates.onOverlayHeight((height) => {
      receivedPush = true;
      setUpdateOverlayHeight(Math.max(0, Math.round(Number(height) || 0)));
    });
    void desktopUpdates
      .getOverlayHeight()
      .then((height) => {
        if (alive && !receivedPush) {
          setUpdateOverlayHeight(Math.max(0, Math.round(Number(height) || 0)));
        }
      })
      .catch(() => {
        // Older/mismatched shells may expose the method before registering IPC.
      });
    return () => {
      alive = false;
      unsubscribe();
    };
  }, [desktopUpdates]);

  // Read early: the conversationId scopes the per-session workspace state
  // (rail open/width/tab/open files) used throughout this component.
  const { conversationId } = useParams<{ conversationId: string }>();
  const [fileViewerCommentsOpen, setFileViewerCommentsOpen] = useState(false);
  const [rightRailTab, setRightRailTab] = useState<RightRailTab>(() =>
    conversationId ? (readSessionWorkspaceState(conversationId).rightRailTab ?? "files") : "files",
  );
  // The comments panel only contributes to the min width when the rail is
  // actually showing the file viewer — on any other tab the FileViewer
  // is unmounted, so the 720 floor would just waste horizontal space. Both
  // the Files and Changes tabs surface the inline viewer, so either qualifies.
  // 240px (CommentsPanel default/min width) + 480px comfortable code viewer
  // width. The panel can be dragged wider, but this floor keeps it usable at
  // its default; widening past it is the user's choice via the inline handle.
  const inlinePanelMinWidth =
    (rightRailTab === "files" || rightRailTab === "changes") && fileViewerCommentsOpen
      ? 720
      : undefined;
  const [searchParams, setSearchParams] = useSearchParams();
  const [sidebarOpen, setSidebarOpen] = useState(initialSidebarOpen);
  const [sidebarPeek, setSidebarPeek] = useState(false);

  // The settings nav lives INSIDE the sidebar, and its "Back" row is the only
  // way off the settings page. A collapsed sidebar therefore strands the user
  // there — the row is still in the DOM but clipped and inert. So entering
  // /settings pins the sidebar open (and drops any peek, which is a transient
  // hover card, not somewhere to read a settings page from), and leaving it
  // restores whatever the sidebar was before, so a collapsed sidebar stays a
  // preference rather than being silently undone by a trip to settings.
  //
  // The pin is only needed WHILE on the page, so restoring on exit cannot
  // reintroduce the trap: by then the title-bar toggle is back and the Back row
  // is no longer the only way out. Mirrors sidebarOpenBeforeMaximizeRef, which
  // stashes and restores the same state around the maximize flow.
  const { inSettings } = useSettingsRoute();
  const sidebarOpenBeforeSettingsRef = useRef<boolean | null>(null);
  useEffect(() => {
    if (inSettings) {
      // Stash inside the updater so it reads the CURRENT value rather than a
      // closure captured before the pin, and so a re-render while already on
      // /settings can't overwrite the stash with the pinned-open value.
      setSidebarOpen((wasOpen) => {
        if (sidebarOpenBeforeSettingsRef.current === null) {
          sidebarOpenBeforeSettingsRef.current = wasOpen;
        }
        return true;
      });
      setSidebarPeek(false);
      return;
    }
    // Leaving /settings: restore, then clear the stash so the next visit
    // captures fresh state.
    if (sidebarOpenBeforeSettingsRef.current !== null) {
      setSidebarOpen(sidebarOpenBeforeSettingsRef.current);
      sidebarOpenBeforeSettingsRef.current = null;
    }
  }, [inSettings]);

  // Reads the same module-level store Sidebar drives, so the rail's ceiling
  // tracks the live sidebar width (including a drag) rather than a guess.
  const { width: sidebarWidth } = useResizableSidebar();
  // ?sidebar=open surfaces the session list on phone-width shells where the
  // sidebar is closed by default — the destination for a "N sessions need
  // your attention" notification tap, which would otherwise land on a bare
  // composer. One-shot: applied then stripped from the URL.
  useEffect(() => {
    if (searchParams.get("sidebar") !== "open") return;
    setSidebarOpen(true);
    const next = new URLSearchParams(searchParams);
    next.delete("sidebar");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  // Live open fraction (0→1) while the iOS edge-swipe drags the sidebar; null
  // when not dragging. Drives the mobile overlay's finger-tracking transform.
  const [sidebarDragProgress, setSidebarDragProgress] = useState<number | null>(null);
  // The iOS shell repurposes the left-edge swipe (normally back-navigation) to
  // drive the sidebar as an interactive drawer, streaming it over the native
  // bridge. begin/move track the finger (mobile overlay only — the desktop
  // width-based sidebar can't be partially slid, so it just settles); open/close
  // are the settle decision on release. No-op outside the iOS shell.
  useEffect(
    () =>
      onNativeSidebarDrag((phase, progress) => {
        if (phase === "open" || phase === "close") {
          setSidebarDragProgress(null);
          setSidebarOpen(phase === "open");
          return;
        }
        if (!isMobileViewport()) return;
        setSidebarDragProgress(progress);
      }),
    [],
  );
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(() =>
    conversationId ? (readSessionWorkspaceState(conversationId).selectedFilePath ?? null) : null,
  );
  // Ordered list of open file tabs. ``selectedFilePath`` is the active tab
  // (null = a scope view, Changed/All, is active). Tabs persist when the user
  // switches to a scope view or another rail tab; only ``closeFile`` removes
  // an entry. Seeded from the per-session store; a ?file= URL param is merged
  // in by the conversation-restore effect below.
  const [openFiles, setOpenFiles] = useState<string[]>(() =>
    conversationId ? (readSessionWorkspaceState(conversationId).openFiles ?? []) : [],
  );
  // ``selectedTerminalKey`` is the active shell tab (mutually exclusive with
  // ``selectedFilePath`` — the rail's content slot shows one thing). The set of
  // tabs itself is not stored: it derives 1:1 from the session's shell
  // inventory (see ``openTerminals`` below), so the tab strip always mirrors the
  // live terminal list. Persisted per-session so a switch/reload restores which
  // shell was focused; the prune effect clears it if that terminal is gone.
  const [selectedTerminalKey, setSelectedTerminalKey] = useState<string | null>(() =>
    conversationId ? (readSessionWorkspaceState(conversationId).selectedTerminalKey ?? null) : null,
  );
  // The one shell key the user just opened by an explicit gesture (clicking a
  // tab / creating a "+"→Shell). Its rail xterm may grab keyboard focus when it
  // connects; a shell merely *restored* on a session switch may not — switching
  // sessions keeps focus in the chat composer. Cleared once consumed and on
  // every session switch.
  const autoFocusTerminalKeyRef = useRef<string | null>(null);
  // Whether the workspace rail is maximized (covers the full content region,
  // hiding the chat column). Session-transient — a fresh visit starts docked.
  const [rightPanelMaximized, setRightPanelMaximized] = useState(false);
  // Sidebar open-state captured when entering full screen, so exiting can
  // restore whatever the user had (collapsed stays collapsed; open reopens).
  const sidebarOpenBeforeMaximizeRef = useRef(false);
  // Scope of the mobile Files drawer: false = full folder tree, true =
  // changed-files-only flat list. On desktop the scope is the selected rail
  // tab (Files vs Changes); on a phone there's no tab strip, so the two FAB
  // entries open this drawer pinned to their scope.
  const [filesDrawerFlatView, setFilesDrawerFlatView] = useState(false);
  // Tracks which conversation the current rail tab / open-files state belongs
  // to, so the persist effect targets the right session even before a switch's
  // restore has re-rendered. Set by the conversation-restore effect.
  const stateConvRef = useRef<string | null>(conversationId ?? null);
  // Skips the first persist run so mount can't write default state over the
  // values the restore effect is about to apply.
  const workspacePersistHydrated = useRef(false);
  // Hidden (dot-prefixed) files are shown by default; the eye toggle hides them.
  const [filesPanelShowHidden, setFilesPanelShowHidden] = useState(true);
  // Lifted so the Changes list order and the FileViewer prev/next order
  // share one source of truth (otherwise the "X/N" index won't match the
  // list position). Default "recent" mirrors the prior FilesPanel default.
  const [filesPanelSort, setFilesPanelSort] = useState<ChangedSort>(
    () => readFilesPanelPreferences().sort,
  );
  const [panelInitialKey, setPanelInitialKeyState] = useState<string | null>(null);
  const [executionLogsKey, setExecutionLogsKey] = useState<string | null>(null);
  const [filesPanelOpen, setFilesPanelOpen] = useState(false);
  // Mobile-only full-screen drawers for the rail tabs that have no desktop
  // push panel of their own. On desktop these are tabs in the workspace rail;
  // on a phone they open as full-screen overlays from the session-menu FAB.
  const [subagentsPanelOpen, setSubagentsPanelOpen] = useState(false);
  const [shellsPanelOpen, setShellsPanelOpen] = useState(false);
  // The right "Workspace" rail (WorkspacePanel) remembers its open/closed
  // state per session. A brand-new session (no saved `open`) follows the
  // Appearance "Workspace panel" default; reopening a session restores how
  // the user last left it. Toggled via the header's PanelRightIcon, mirroring
  // the sidebar collapse. With no conversation the rail can't render, so the
  // state stays false — leaving it true would let rail-gated side effects fire
  // on non-session routes like the home page.
  const [rightPanelOpen, setRightPanelOpen] = useState(() =>
    conversationId
      ? (readSessionWorkspaceState(conversationId).open ?? readDefaultWorkspacePanelOpen())
      : false,
  );
  const [shareOpen, setShareOpen] = useState(false);
  const [forkOpen, setForkOpen] = useState(false);
  // Truncation point for a "fork from here" opened from a message's
  // actions (ChatPage, via ForkDialogContext). `null` = full clone —
  // the mobile menu Clone entry's behavior. Cleared whenever the dialog
  // opens fresh or closes, so a later Clone doesn't silently truncate.
  const [forkUpToResponseId, setForkUpToResponseId] = useState<string | null>(null);
  // Agent tools & policies dialog — the mobile counterpart of the desktop
  // AgentInfoButton popover, opened from the header's three-dot menu.
  const [agentInfoOpen, setAgentInfoOpen] = useState(false);
  // Single source of truth for "terminal view on" — toggle and rail
  // both route through setPanelInitialKey so they can't disagree.
  const panelOpen = panelInitialKey !== null;
  const executionLogsOpen = executionLogsKey !== null;
  const fileViewerOpen = selectedFilePath !== null;
  // Drives the Terminal-pill spinner for terminal-first sessions while
  // the runner is auto-creating the terminal. Surfaced via
  // TerminalFirstContext below.
  const terminalPending = useChatStore((s) => s.terminalPending);
  // Read the conversation's terminals here too so the FAB's dropdown
  // can show/hide its "Terminals" entry and route a click to the first
  // terminal. The hook is react-query-backed and dedup'd with the rail.
  // reconcileWhilePending: self-heals if the live resource.created SSE was
  // missed (see UseTerminalsOptions for the why).
  const { terminals } = useTerminals(conversationId ?? null, {
    reconcileWhilePending: terminalPending,
  });
  const agentTerminal = useMemo(() => findAgentTerminal(terminals), [terminals]);

  const debugMode = useDebugMode();
  const { data: conversationsData, isLoading: conversationsLoading } = useConversations("", true);
  // Surface sessions needing attention as OS notifications + a dock badge.
  // Mounted here (inside the Router) so it can navigate on click and knows
  // the active conversation id, which suppresses the notification/badge for
  // the session the user is actively viewing.
  useIdleNotifications(conversationId);
  // Seed the per-user read-state (unread/seen) mirror from the conversation
  // list, so the sidebar dots reflect what the user did on any device.
  // `undefined` while the query is still loading (vs `[]` for a loaded-but-
  // empty list) so the seed — and the `hydrated` gate it flips — waits for
  // the real read-state, not the transient empty list on a deep-link/reload.
  const allConversations = useMemo(
    () => conversationsData?.pages.flatMap((p) => p.data),
    [conversationsData],
  );
  useSeedReadState(allConversations);
  const activeConv = useMemo(() => {
    if (!conversationId) return null;
    return (
      conversationsData?.pages.flatMap((p) => p.data).find((c) => c.id === conversationId) ?? null
    );
  }, [conversationId, conversationsData]);
  // Single-conversation snapshot (shared cache with chatStore.bindStream).
  // For sub-agent (child) sessions the sidebar list omits the row, so this
  // is the only path through which the UI learns the user's permission
  // level. ``derivePermissionLevel`` prefers this over ``activeConv``.
  const { session: activeSession, isLoading: sessionLoading } = useSession(conversationId);
  // Same liveness the chat surface switches on (see ChatPage / useSessionLiveness).
  // AppShell reads it only to drive the Terminal pill's "loading" state: a session
  // in `starting` (a relaunch the moment a message is sent — `turnActive`) is
  // coming up. Snapshot fallback so an off-sidebar/child session still gets its
  // `host_id`.
  const chatStatus = useChatStore((s) => s.status);
  // Server session status (driven by `session.status` SSE + snapshot). A
  // `failed` session suppresses the terminal-startup spinner so a crashed
  // runner shows only the error banner, not a spinner that can never resolve.
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  // Set when this client launches a runner outside the send path (a host
  // switch); extends the liveness startup grace so the move spins.
  const runnerLaunchedAt = useChatStore((s) => s.runnerLaunchedAt);
  const livenessRow = activeConv ?? livenessRowFromSession(activeSession);
  const liveness = useSessionLiveness(conversationId ?? undefined, livenessRow, {
    turnActive: chatStatus === "streaming",
    launchedAt: runnerLaunchedAt,
  });
  // Full agent object (mcp_servers + policies) for the header info icon.
  // react-query-cached, so this shares the fetch ChatPage's picker makes.
  const { data: boundAgent } = useSessionAgent(conversationId ?? null);
  const permissionLevel = derivePermissionLevel(
    activeSession,
    sessionLoading,
    activeConv,
    conversationId,
    conversationsData !== undefined,
  );
  // Labels can come from the sidebar row (``activeConv``) for top-level
  // sessions OR the per-session snapshot (``activeSession``) for ALL
  // sessions including children. The sidebar list omits child (sub-agent)
  // rows, so for a user-added agent ``activeConv`` is null and only the
  // snapshot carries ``omnigent.ui``/``omnigent.wrapper`` — without
  // this merge an added claude-native agent loses its terminal-first
  // toggle. Snapshot wins on conflict; spreading undefined is a no-op.
  const sessionLabels = { ...activeConv?.labels, ...activeSession?.labels };
  const terminalFirst = sessionLabels["omnigent.ui"] === "terminal";
  const isClaudeNative = sessionLabels["omnigent.wrapper"] === "claude-code-native-ui";
  // Native-CLI wrapper of either family. Keys harness behavior gates
  // (composer slash commands, `/model`); terminal-first SDK sessions
  // (embedded Omnigent REPL terminal) have NO wrapper label and must
  // keep regular chat behavior. See TerminalFirstContext.tsx.
  const isNativeWrapper = isNativeWrapperLabel(sessionLabels["omnigent.wrapper"]);
  // Used for the header "Back to parent" link, which is hidden on
  // top-level sessions. The Subagents tab itself is always visible —
  // it lists the root's children plus a "main" entry, so the user
  // can move between siblings and back to the parent from one place.
  const isChildSession = activeSession?.parentSessionId != null;
  // A sub-agent runs in its parent's working directory but records no
  // host/workspace of its own (the parent owns the tmux pane), which would drop
  // the fork dialog into its no-directory mode. The parent's sidebar row backs
  // the child's missing values so promoting a child offers the same host +
  // directory choices as forking any other session.
  const parentConv = useMemo(
    () => allConversations?.find((c) => c.id === activeSession?.parentSessionId) ?? null,
    [allConversations, activeSession?.parentSessionId],
  );
  // ── Header breadcrumb ─────────────────────────────────────────────────
  // The chat header shows the conversation's title, prefixed by a folder icon
  // when the session is filed under a project, and with the sub-agent identity
  // appended when viewing a child. Title + project come from the active
  // conversation normally, or its immediate parent when inside a sub-agent
  // (whose own row the sidebar omits, so its title falls back to the parent
  // snapshot). The parent title also links back out, replacing the old "Back"
  // button. A child always gets a title (fallback "New session") so that
  // climb-out does not wait on the parent row or snapshot. Prefer a real
  // list/snapshot title over conversationDisplayLabel — that helper's
  // "New session" fallback would otherwise shadow a titled snapshot.
  // Project membership lives only on the list row (`Conversation`), never
  // the snapshot, so a parent outside the loaded window shows no folder.
  const { session: parentSession } = useSession(activeSession?.parentSessionId);
  const { data: projectSummaries } = useProjects();
  const breadcrumbConv = isChildSession ? parentConv : activeConv;
  const headerConversationTitle =
    breadcrumbConv?.title ||
    (isChildSession ? parentSession?.title : activeSession?.title) ||
    (breadcrumbConv ? conversationDisplayLabel(breadcrumbConv) : null) ||
    (isChildSession ? UNTITLED_CONVERSATION_LABEL : null);
  const headerProjectName =
    (breadcrumbConv?.project_id != null
      ? projectSummaries?.find((p) => p.id === breadcrumbConv.project_id)?.name
      : undefined) ??
    breadcrumbConv?.labels?.[PROJECT_LABEL_KEY] ??
    null;
  const headerTitleLinkTo =
    isChildSession && activeSession?.parentSessionId
      ? `/c/${activeSession.parentSessionId}`
      : undefined;
  // Positive "this is a top-level session" signal for the top-level-only
  // actions (Share/Clone). Gating those on ``!isChildSession`` flickered:
  // while the snapshot loads ``activeSession`` is null, so ``isChildSession``
  // is false and the buttons rendered, then vanished once the snapshot
  // revealed ``parentSessionId``. A session present in the sidebar list is
  // known top-level immediately (children are omitted from that query);
  // otherwise we wait for the snapshot rather than optimistically showing.
  const activeSessionMatchesRoute = activeSession?.id === conversationId;
  const isKnownTopLevel =
    activeConv != null ||
    (activeSessionMatchesRoute && activeSession != null && activeSession.parentSessionId == null);
  const actionConversation = useMemo<Conversation | null>(() => {
    if (!isKnownTopLevel || !isOwnerLevel(permissionLevel)) return null;
    if (activeConv) return activeConv;
    if (!activeSessionMatchesRoute || !activeSession) return null;
    return {
      id: activeSession.id,
      object: "conversation",
      title: activeSession.title,
      created_at: activeSession.createdAt,
      updated_at: activeSession.createdAt,
      labels: activeSession.labels ?? {},
      permission_level: activeSession.permissionLevel,
      runner_id: activeSession.runnerId ?? null,
      host_id: activeSession.hostId ?? null,
      workspace: activeSession.workspace ?? null,
      agent_id: activeSession.agentId,
      agent_name: activeSession.agentName,
      git_branch: activeSession.gitBranch ?? null,
    };
  }, [activeConv, activeSession, activeSessionMatchesRoute, isKnownTopLevel, permissionLevel]);
  // Header action gating, hoisted so the desktop buttons and the mobile
  // three-dot menu render the exact same set (they can't drift apart).
  // Stop session is not a header action — it lives in the sidebar row's
  // kebab menu (see Sidebar's ConversationRow).
  // Only the owner can manage sharing; top-level only. Sharing a
  // sub-agent is a no-op anyway — children inherit the parent's grants via
  // the server's parent-delegation path — so we hide the affordance.
  // Also hidden in single-user mode: with no other users to grant to, the
  // affordance is meaningless (unlike the local-server / sharing-off cases
  // below, which stay present-but-disabled with an explanatory tooltip).
  // ``isOwnerLevel`` is permissive on a null level (single-user / still
  // loading), matching the sidebar's owner-only Share gate and the terminal
  // ``readOnly`` gate below; the authoritative snapshot level resolves it.
  const serverInfo = useServerInfo();
  const canShare =
    !!conversationId &&
    isKnownTopLevel &&
    isOwnerLevel(permissionLevel) &&
    !isSingleUserMode(serverInfo);
  // Two independent reasons the Share affordance is present-but-disabled: a
  // local server can't produce openable links, and a deployed server whose
  // admin set OMNIGENT_SHARING_MODE=off reports sharing_mode "off" via
  // /v1/info. Fail open (share enabled) while the capability probe loads.
  const sharingOff = serverInfo !== "loading" && serverInfo.sharing_mode === "off";
  const shareDisabled = canShare && (isCurrentServerLocal() || sharingOff);
  const shareDisabledReason = !shareDisabled
    ? undefined
    : isCurrentServerLocal()
      ? "Sharing is unavailable from a local server."
      : "Sharing has been disabled for this Omnigent server.";
  // Any viewer can fork a shared session, sub-agents included — forking a
  // child is how it gets promoted to a top-level session of its own. Gated on
  // knowing which the session is (sidebar row or loaded snapshot) so the
  // affordance doesn't flicker in before that resolves. Surfaced as
  // ForkDialogContext.canFork — the per-message "Fork from here" action is
  // the only fork entry point.
  const canClone =
    !!conversationId &&
    (isKnownTopLevel || isChildSession) &&
    (permissionLevel === null || permissionLevel >= 1);
  // Agent tools/policies exist to show.
  const hasAgentInfo = !!conversationId && agentHasInfo(boundAgent, conversationId);
  // Whether the mobile three-dot menu has any entry to offer.
  const hasHeaderMenu = canShare || hasAgentInfo;
  // The live snapshot is authoritative; the sidebar row is only a fallback
  // (it is absent entirely for sub-agent children, which the list omits).
  const wrapperLabel =
    activeSession?.labels?.[WRAPPER_LABEL_KEY] ?? activeConv?.labels?.[WRAPPER_LABEL_KEY] ?? null;
  // Claude-native sub-agents have no terminal of their own — the parent
  // owns the tmux pane.
  const isClaudeNativeSubagent = wrapperLabel === "claude-code-native-ui-subagent";
  // Hide shells (the mobile Shells drawer entry) only for claude-native
  // sub-agents — they have no terminals of their own (the parent owns the
  // tmux pane). Native top-level sessions get shells like SDK ones; their
  // vendor pane is excluded from the inventory like the SDK REPL (see
  // ``inventoryTerminals``).
  const hideTerminalsTab = isClaudeNativeSubagent;
  // Inventory view of the terminal list for the rail tab, its badge,
  // and the mobile menu. The pill surfaces (``terminalsAvailable``,
  // ``setView``) keep the full ``terminals`` list so the agent's own
  // terminal stays openable through the Terminal pill.
  const railTerminals = useMemo(
    () => inventoryTerminals(terminals, terminalFirst),
    [terminals, terminalFirst],
  );
  // Shell soft tabs are 1:1 with the session's shell inventory: every shell —
  // whether the user opened it or the agent spawned it — is its own tab, and a
  // shell that goes away (killed via a tab close, closed by the agent, or the
  // runner emptied the list) drops from ``railTerminals`` and takes its tab with
  // it. The agent's own control terminal is excluded by ``inventoryTerminals``
  // (it lives behind the connection pill, not as a shell tab).
  const openTerminals = useMemo(() => railTerminals.map((t) => terminalTabKey(t)), [railTerminals]);
  const deleteTerminalMutation = useDeleteTerminal(conversationId ?? "");
  // Tab key awaiting a close confirmation, or null. Closing a tab kills the
  // terminal, so a user-initiated close routes through a confirm modal first.
  const [terminalPendingClose, setTerminalPendingClose] = useState<string | null>(null);
  // While a close is in flight, grey the tab being killed so the delay between
  // confirming and the tab disappearing (the DELETE round-trip + its
  // ``resource.deleted``) reads as "closing", not frozen.
  const closingTerminalKey =
    deleteTerminalMutation.isPending && deleteTerminalMutation.variables
      ? `terminal:${deleteTerminalMutation.variables}`
      : null;
  // A user-initiated "+"→Shell create: snapshot the shell tabs present when it
  // starts so the effect below can focus the newly-created shell the instant
  // its tab lands in the list. That's decoupled from the create POST, which on
  // a waking (runner-asleep) session resolves well after the reconcile poll /
  // SSE has already surfaced the tab.
  const pendingShellCreateRef = useRef<Set<string> | null>(null);
  const markShellCreateStarted = useCallback(() => {
    pendingShellCreateRef.current = new Set(openTerminals);
  }, [openTerminals]);
  // The create POST failed — disarm the focus snapshot so a later
  // agent-spawned shell isn't mistaken for the one the user tried to open.
  const clearShellCreatePending = useCallback(() => {
    pendingShellCreateRef.current = null;
  }, []);
  // Whether the agent's spec declares shell access (a ``terminals:`` block).
  // The desktop rail's Shells TAB shows only once a shell exists (creation is
  // via the tab-strip "+" menu), but the MOBILE Shells drawer — which has no
  // "+" menu — must be reachable at zero shells so its "+ New shell" row is the
  // create entry point. So mobile gates on declared access too.
  const agentSupportsShells = (boundAgent?.terminals ?? []).length > 0;
  // The "root" session for the Subagents tab. The rail renders the whole
  // spawn tree from the top-level session, so when the user is inside a
  // descendant we walk the parent chain to the top via ``useRootSessionId``
  // (cache-backed — usually zero network for trees the user navigated).
  //
  // Sticky resolution: while the session snapshot loads ``activeSession``
  // is null, and while the parent walk runs ``walkedRoot`` is null. A naive
  // fallback momentarily re-roots the rail at the clicked session (or its
  // immediate parent), collapsing the tree for one render, flipping
  // ``showSubagentsTab`` false, and yanking the user off Agents onto Files.
  // Instead, when the navigation target is a known member of the last
  // root's cached tree, we hold that root until the authoritative
  // resolution lands (a no-op transition once it does).
  const stickyRootRef = useRef<string | null>(null);
  const queryClient = useQueryClient();
  const walkedRoot = useRootSessionId(conversationId ?? null, activeSession?.parentSessionId);
  const { rootSessionId, rootSessionResolved } = useMemo(() => {
    if (!conversationId) return { rootSessionId: null, rootSessionResolved: false };
    // Snapshot resolved for a top-level session → it is its own root.
    if (activeSession && activeSession.parentSessionId == null) {
      return { rootSessionId: conversationId, rootSessionResolved: true };
    }
    // Snapshot resolved for a descendant + walk complete → authoritative.
    if (activeSession && walkedRoot) {
      return { rootSessionId: walkedRoot, rootSessionResolved: true };
    }
    // Snapshot or walk still loading → hold the last root while the target
    // is that root itself or a known member of its cached tree.
    const sticky = stickyRootRef.current;
    if (
      sticky !== null &&
      (sticky === conversationId ||
        cachedTreeContains(queryClient, sticky, conversationId, MAX_TREE_DEPTH))
    ) {
      return { rootSessionId: sticky, rootSessionResolved: true };
    }
    // No sticky context (e.g. a deep link straight into a sub-agent):
    // fall back one hop until the walk resolves the true root.
    return {
      rootSessionId: activeSession?.parentSessionId ?? conversationId,
      rootSessionResolved: false,
    };
  }, [conversationId, activeSession, walkedRoot, queryClient]);
  // One-shot fetch (no polling) for the Subagents tab's count badge.
  // SubagentsPanel mounts its own polling usage of the hook against
  // the same rootSessionId, so the cache is shared.
  const { children: childSessions } = useChildSessions(rootSessionId);
  // Remember the resolved root so a later click into one of its tree
  // members can hold it steady (see ``rootSessionId`` above).
  useEffect(() => {
    if (rootSessionResolved) stickyRootRef.current = rootSessionId;
  }, [rootSessionId, rootSessionResolved]);
  const { panelWidth: inlinePanelWidth, handleProps: inlinePanelHandleProps } =
    useResizableInlinePanel(
      rootSessionId,
      inlinePanelMinWidth,
      sidebarOpen ? sidebarWidth : 0,
      rootSessionResolved,
    );
  // How many children are actively working — surfaced in the tab badge so
  // "something's happening" is visible without opening the panel.
  const subagentsWorking = childSessions.filter((c) => c.busy).length;
  // Total agents in the session tree, main agent included — the Agents
  // tab badge starts at 1 for a lone agent and the tab is ALWAYS shown
  // (the panel's "main" row links back to the root, so an empty tree is
  // a one-entry list, not a dead end).
  const agentCount = childSessions.length + 1;

  // Hide the files panel entirely when the agent spec has no os_env. Probe
  // the default environment resource instead of the root filesystem listing:
  // it is enough to prove availability without paying for directory contents.
  const environmentQuery = useWorkspaceEnvironment(conversationId);
  const showFilesPanel = environmentQuery.data?.available !== false;
  // Per-tab availability for the right workspace rail — the single source
  // of truth shared by the tab-fallback effect below, the rail's mount
  // gate, and the header's collapse toggle, so they can never disagree.
  const railTabsAvailable = useMemo(
    () =>
      ({
        files: showFilesPanel,
        // Changes tab shares the Files gate — same on-disk workspace, just the
        // changed-files scope.
        changes: showFilesPanel,
        // Browser tab: shown only when the desktop shell hosts the embedded
        // WebContentsView. A plain web build has no embedded browser, and an
        // older desktop build predates the `browser*` bridge — both hide the
        // tab entirely (supportsBrowser() is constant per load) so we never
        // surface a dead tab whose calls no-op.
        browser: supportsBrowser(),
        // Agents tab is unconditional: the panel always lists at least
        // the main agent (its "main" row), so there's never a dead end.
        subagents: true,
        // Shells have no nav tab — they open as closable soft tabs in the
        // rail's tab strip (see WorkspacePanel's TerminalTabsStrip / "+"
        // menu). Mobile keeps a shells drawer (see ``showShellsTab`` below).
      }) as const,
    [showFilesPanel],
  );
  // Whether the rail has anything at all to show. When false the workspace
  // card doesn't mount and the header hides its collapse toggle — a
  // no-filesystem agent with no terminals/sub-agents would otherwise
  // render an empty white card with no way to dismiss it.
  const hasRailContent = Object.values(railTabsAvailable).some(Boolean);
  // Keep the selected tab valid. When the current tab disappears — e.g. the
  // files panel turns off — fall back to the first still-visible tab in
  // display order (Files · Changes · Agents · Browser). Picking
  // the first available (rather than ping-ponging between two effects) keeps
  // this convergent even when several tabs vanish at once.
  useEffect(() => {
    if (railTabsAvailable[rightRailTab]) return;
    const next = (["files", "changes", "subagents", "browser"] as const).find(
      (t) => railTabsAvailable[t],
    );
    if (next) setRightRailTab(next);
  }, [railTabsAvailable, rightRailTab]);

  // Mount the relay at the always-present shell level (not BrowserPane, which
  // only mounts while its tab is selected) so it's listening before the first
  // browser_navigate. No-op outside Electron / with no conversation.
  useBrowserAgentRelay(conversationId);

  // Clear a stale browser-view suppression left by a renderer reload/crash: the
  // main-process flag persists but this renderer's overlay count reset to 0, so
  // a dialog open across the reload would strand the pane hidden. Re-assert our
  // real state once on mount. No-op outside a browser-capable shell.
  useEffect(() => {
    resyncBrowserSuppression();
  }, []);

  // Auto-surface the Browser tab on a `navigate` action, so a browser_navigate
  // fired while another tab is selected doesn't load into a hidden pane.
  // Browser-capable shells only; no-op elsewhere (the bus never fires without a relay).
  useEffect(() => {
    if (!supportsBrowser()) return;
    return onBrowserActionRequest((evt) => {
      if (evt.action !== "navigate") return;
      setRightRailTab("browser");
      setRightPanelOpen(true);
    });
  }, []);

  // Design-mode submit routing. Lives here (with the hoisted relay) because the
  // in-page popup posts back via preload IPC delivered to the always-mounted
  // shell, not BrowserPane. On submit: build the `[Design Mode — …]` message,
  // attach the cropped screenshot, send via the NORMAL chat path (no backend
  // route), then signal the result back for green/red. Dismiss is a no-op.
  // Routes to the conversation's own bound agent (the picked element belongs to
  // the page it drives). The screenshot arrives on the earlier element-selected
  // event, so we stash the latest per conversation and pair it at submit time.
  const designShotRef = useRef<Map<string, string>>(new Map());
  const boundAgentId = boundAgent?.id ?? null;
  useEffect(() => {
    if (!supportsBrowser()) return;
    const w = window as unknown as {
      omnigentDesktop?: {
        onBrowserElementSelected?: (
          cb: (p: { conversationId?: string; screenshot?: string | null }) => void,
        ) => () => void;
        onBrowserElementPromptSubmit?: (
          cb: (p: {
            conversationId?: string;
            id?: number;
            element?: DesignModeElement;
            prompt?: string;
          }) => void,
        ) => () => void;
        onBrowserElementPromptDismiss?: (
          cb: (p: { conversationId?: string }) => void,
        ) => () => void;
        browserSignalDesignResult?: (
          conversationId: string,
          result: { id: number; ok: boolean; message?: string },
        ) => Promise<{ ok: boolean; error?: string }>;
      };
    };
    const desktop = w.omnigentDesktop;
    if (!desktop) return;

    const unsubSelected = desktop.onBrowserElementSelected?.((payload) => {
      const cid = payload.conversationId;
      if (!cid) return;
      if (typeof payload.screenshot === "string") {
        designShotRef.current.set(cid, payload.screenshot);
      } else {
        designShotRef.current.delete(cid);
      }
    });

    const unsubSubmit = desktop.onBrowserElementPromptSubmit?.((payload) => {
      const cid = payload.conversationId;
      const submitId = typeof payload.id === "number" ? payload.id : 0;
      const signal = (ok: boolean, message: string) => {
        if (cid) void desktop.browserSignalDesignResult?.(cid, { id: submitId, ok, message });
      };
      if (!cid || !payload.element || !payload.prompt) {
        signal(false, "Missing element or prompt.");
        return;
      }
      if (!boundAgentId) {
        signal(false, "No agent bound to this session yet.");
        return;
      }
      try {
        const text = buildDesignModePrompt(payload.element, payload.prompt);
        const shot = designShotRef.current.get(cid);
        const file = dataUrlToFile(shot, `design-element-${submitId}.png`);
        void useChatStore
          .getState()
          .send(text, boundAgentId, file ? [file] : undefined)
          .then(() => signal(true, "Sent to agent."))
          .catch((err: unknown) => signal(false, `Send failed: ${String(err)}`));
        // Clear the stashed screenshot so a later submit without a fresh pick
        // doesn't reuse a stale crop.
        designShotRef.current.delete(cid);
      } catch (err) {
        signal(false, `Error: ${String(err)}`);
      }
    });

    // Dismiss is a no-op on the React side — the in-page popup tears its own
    // UI down; we just don't want an unhandled subscription.
    const unsubDismiss = desktop.onBrowserElementPromptDismiss?.(() => {});

    return () => {
      unsubSelected?.();
      unsubSubmit?.();
      unsubDismiss?.();
    };
  }, [boundAgentId]);

  // Build a stable Set of agent-changed file paths so the FileViewer context
  // can tell BlockRenderer which inline code spans are real workspace files.
  // We use the changed-files list (not the flat top-level directory listing)
  // because it contains full relative paths like `web/src/shell/Foo.tsx`.
  const changedFilesQuery = useWorkspaceChangedFiles(conversationId);
  const changedFilePaths = useMemo(
    () => new Set(changedFilesQuery.data?.data.map((f) => f.path) ?? []),
    [changedFilesQuery.data],
  );
  const changedCount = changedFilesQuery.data?.data.length ?? 0;
  const isChangedPath = useCallback(
    (path: string) => changedFilePaths.has(path),
    [changedFilePaths],
  );

  // Persist the Chat/TUI toggle position per-conversation so leaving and
  // re-entering a native session restores the view the user chose instead of
  // reapplying the global default. Keep the explicit choice in the URL too so
  // a refresh or shared link restores either surface.
  const setPanelInitialKey = useCallback(
    (key: string | null) => {
      setPanelInitialKeyState(key);
      if (conversationId) {
        const storageKey = `omnigent.web.panel-key:${conversationId}`;
        sessionStorage.setItem(storageKey, key === null ? CHAT_VIEW_STORAGE_VALUE : key);
      }
      if (terminalFirst) {
        setSearchParams(
          (prev) => {
            const next = new URLSearchParams(prev);
            next.set("view", key === null ? "chat" : "terminal");
            return next;
          },
          { replace: true },
        );
      }
    },
    [conversationId, setSearchParams, terminalFirst],
  );

  // Restore the per-session workspace state when switching conversations:
  // rail open-state, selected tab, and the open file tabs. The Chat/TUI toggle
  // is restored from sessionStorage; with no per-tab choice, terminal-first
  // sessions use the Appearance default. The Files scope and the active file
  // also honor URL search params so shared links open to the right view.
  useEffect(() => {
    setExecutionLogsKey(null);
    setFilesPanelOpen(false);
    setSubagentsPanelOpen(false);
    setShellsPanelOpen(false);
    setFilesPanelShowHidden(true);
    // Drop shell interaction state carried from the outgoing session: a
    // still-armed create ref would otherwise auto-focus an unrelated shell in
    // the new session, and an open "Close shell?" dialog would target a
    // terminal absent from the new session's list.
    pendingShellCreateRef.current = null;
    setTerminalPendingClose(null);
    if (!conversationId) {
      // No session → no rail; false (not the open default) so rail-gated
      // effects stay quiet on non-session routes.
      setRightPanelOpen(false);
      setRightRailTab("files");
      setSelectedFilePath(null);
      setOpenFiles([]);
      autoFocusTerminalKeyRef.current = null;
      setSelectedTerminalKey(null);
      setPanelInitialKeyState(null);
      stateConvRef.current = null;
      return;
    }
    const persisted = readSessionWorkspaceState(conversationId);

    const storageKey = `omnigent.web.panel-key:${conversationId}`;
    const stored = sessionStorage.getItem(storageKey);
    const requestedView = terminalFirst ? searchParams.get("view") : null;
    const terminalKey =
      agentTerminal === null ? PANEL_NO_TERMINAL_KEY : terminalTabKey(agentTerminal);
    const defaultToTerminal = terminalFirst && readTranscriptViewDefault() === "terminal";
    setPanelInitialKeyState(
      requestedView === "chat"
        ? null
        : requestedView === "terminal"
          ? resolveTerminalViewKey(stored, terminalKey)
          : stored === CHAT_VIEW_STORAGE_VALUE
            ? null
            : (stored ?? (defaultToTerminal ? terminalKey : null)),
    );

    // Restore the selected rail tab (the Files vs Changes scope is now the tab
    // itself). ``nextTab`` stays null when there's no persisted tab and no file
    // to surface, so the tab-fallback effect can still land on the first
    // *available* tab — forcing "files" here would shadow it.
    let nextTab: RightRailTab | null = persisted.rightRailTab ?? null;

    // Restore the open file tabs from the per-session store, then merge the
    // URL ?file= param: a deep-link selects (and, if absent, opens) that file
    // without discarding the other remembered tabs.
    const fileParam = searchParams.get("file");
    const urlFile = fileParam === null || fileParam === "" ? null : fileParam;
    const persistedFiles = persisted.openFiles ?? [];
    const nextOpenFiles =
      urlFile && !persistedFiles.includes(urlFile) ? [...persistedFiles, urlFile] : persistedFiles;
    const nextSelected = urlFile ?? persisted.selectedFilePath ?? null;
    setOpenFiles(nextOpenFiles);
    setSelectedFilePath(nextSelected);
    // The tab strip derives from the live terminal list, so there's nothing to
    // restore for the tabs themselves — only which shell was focused. The prune
    // effect clears that selection if its terminal no longer exists once this
    // session's list loads. A restored shell selection must not coexist with a
    // file selection (one content slot).
    autoFocusTerminalKeyRef.current = null;
    setSelectedTerminalKey(nextSelected ? null : (persisted.selectedTerminalKey ?? null));
    // A maximized rail is transient too — the incoming session starts docked.
    // If we were maximized, restore the sidebar we collapsed on entry (the
    // toggle handler won't run on a session switch).
    setRightPanelMaximized((wasMaximized) => {
      if (wasMaximized) restoreSidebarAfterMaximize();
      return false;
    });
    // A selected file must be visible in the rail. The Files and Changes tabs
    // both surface the inline viewer; the Agents/Browser tabs don't, so
    // pull the rail to Files unless it's already on a files scope.
    if (nextSelected && nextTab !== "files" && nextTab !== "changes") {
      nextTab = "files";
    }
    if (nextTab !== null) setRightRailTab(nextTab);

    // Restore the rail open-state for this session. A deep link / reload that
    // carries a workspace signal — a file to open (?file=) or a comment to
    // surface (?comment=) — reveals the rail even when this session was last
    // left closed; otherwise the linked file/comment would render into a
    // collapsed, invisible panel. This is transient: it doesn't rewrite the
    // session's saved open-state.
    const commentParam = searchParams.get("comment");
    const hasWorkspaceUrlSignal =
      urlFile !== null || (commentParam !== null && commentParam !== "");
    setRightPanelOpen((persisted.open ?? readDefaultWorkspacePanelOpen()) || hasWorkspaceUrlSignal);

    stateConvRef.current = conversationId;
  }, [conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Session metadata can arrive after the restore effect above. Once the
  // terminal-first label is known, apply URL state first, then the per-tab
  // choice, then the configured default.
  useEffect(() => {
    if (!conversationId || !terminalFirst) return;
    const requestedView = searchParams.get("view");
    const stored = sessionStorage.getItem(`omnigent.web.panel-key:${conversationId}`);
    const terminalKey =
      agentTerminal === null ? PANEL_NO_TERMINAL_KEY : terminalTabKey(agentTerminal);
    if (requestedView === "chat") {
      setPanelInitialKeyState(null);
    } else if (requestedView === "terminal") {
      setPanelInitialKeyState(resolveTerminalViewKey(stored, terminalKey));
    } else if (stored === CHAT_VIEW_STORAGE_VALUE) {
      setPanelInitialKeyState(null);
    } else if (stored !== null) {
      setPanelInitialKeyState(stored);
    } else if (readTranscriptViewDefault() === "terminal") {
      setPanelInitialKeyState(terminalKey);
    }
  }, [agentTerminal, conversationId, searchParams, terminalFirst]);

  // Persist the per-session rail tab + open file tabs whenever they change.
  // Keyed on the state (not conversationId) and targeted at the conversation
  // the current state belongs to (stateConvRef, set by the restore effect) so
  // a conversation switch can't write the outgoing session's state into the
  // incoming one. The first run (mount) is skipped so it can't clobber the
  // store with defaults before the restore effect's values are applied.
  useEffect(() => {
    if (!workspacePersistHydrated.current) {
      workspacePersistHydrated.current = true;
      return;
    }
    const id = stateConvRef.current;
    if (!id) return;
    writeSessionWorkspaceState(id, {
      rightRailTab,
      openFiles,
      selectedFilePath,
      selectedTerminalKey,
    });
  }, [rightRailTab, openFiles, selectedFilePath, selectedTerminalKey]);

  const handleFilesSortChange = useCallback((s: ChangedSort) => {
    setFilesPanelSort(s);
    writeFilesPanelPreferences({ ...readFilesPanelPreferences(), sort: s });
  }, []);

  const openFileViewer = useCallback(
    (path: string) => {
      setSelectedFilePath(path);
      // A file and a shell tab can't both own the rail's content slot —
      // opening a file deselects any active shell tab (its tab stays in the
      // strip).
      setSelectedTerminalKey(null);
      // Add the path to the open tabs if it isn't already open; activating an
      // already-open tab just re-selects it (no duplicate).
      setOpenFiles((prev) => (prev.includes(path) ? prev : [...prev, path]));
      // Close the terminal drawer so the file viewer is unobscured —
      // but only in non-terminal-first sessions, where opening a file
      // and viewing the terminal compete for the same rail slot. In
      // terminal-first sessions the terminal renders inline in main
      // (no drawer) and the rail stays visible alongside it, so we
      // must NOT reset `panelInitialKey` here — that would silently
      // flip the connection-pill view back to Chat (#bug from PR
      // review: clicking a file collapsed Terminal view).
      if (!terminalFirst) {
        setPanelInitialKey(null);
      }
      setExecutionLogsKey(null); // close execution-logs panel
      setFilesPanelOpen(false); // close files drawer so the viewer is unobscured
      setSubagentsPanelOpen(false); // close mobile agents drawer
      // Pull the rail to the Files tab when parked on a tab where the viewer
      // won't render (Subagents). The Files tab surfaces the
      // FileViewer inline, so leave it undisturbed.
      setRightRailTab((prev) => (prev === "subagents" ? "files" : prev));
      // Reveal the rail so the viewer is actually visible — a session the user
      // collapsed (or one that started collapsed via the Appearance default)
      // would otherwise route the file into an invisible panel. Persist
      // open=true so the rail stays in sync with the open file on the next
      // visit (mirroring the header toggle's persistence).
      setRightPanelOpen(true);
      if (conversationId) writeSessionWorkspaceState(conversationId, { open: true });
      // Set URL in the callback (not a useEffect) to avoid racing with
      // FileViewer's diff-sync effect which can clobber it on mount.
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("file", path);
          next.delete("comment"); // stale comment belongs to the previous file
          return next;
        },
        { replace: true },
      );
    },
    [setPanelInitialKey, terminalFirst, setSearchParams, conversationId],
  );

  // Strip the file-viewer URL params (file/diff/comment). Memoized on
  // ``setSearchParams`` so it always closes over react-router's *current*
  // ``navigate`` — which is bound to the live ``locationPathname`` — rather
  // than a stale one captured at first mount (see ``showScopeView`` below).
  const clearFileViewerUrl = useCallback(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("file");
        next.delete("diff");
        next.delete("comment");
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  // Toggle the right (Workspace) sidebar — shared by the header's collapse
  // button and the ⌘⌥]/Ctrl+Alt+] hotkey so they can't drift. Beyond flipping the
  // open-state it persists the choice and keeps the deep-link URL in sync:
  // re-add ?file= on reopen (the FileViewer diff-sync race makes an effect
  // unsafe here), and strip file/diff/comment on collapse so the URL never
  // advertises a panel that isn't shown.
  const toggleRightPanel = () => {
    const next = !rightPanelOpen;
    if (conversationId) {
      writeSessionWorkspaceState(conversationId, { open: next });
      // Remember the choice app-wide as well, so a collapsed rail stays
      // collapsed in the next chat the user opens (and on reload) until they
      // reopen it. Sessions with their own saved open-state still win over
      // this. Guarded on a conversation like the per-session write: off a
      // session route the rail can't render, so the hotkey's flip is inert and
      // must not rewrite the remembered state.
      writeDefaultWorkspacePanelOpen(next);
    }
    if (next) {
      if (selectedFilePath) {
        // Reopening lands back on the file remembered in per-session
        // state, so re-add ?file= to keep the URL shareable. diff and
        // comment are URL-only ephemerals (not remembered), so they
        // intentionally don't rehydrate. Imperative (not an effect) to
        // avoid the FileViewer diff-sync race documented in that effect.

        setSearchParams(
          (prev) => {
            const params = new URLSearchParams(prev);
            params.set("file", selectedFilePath);
            return params;
          },
          { replace: true },
        );
      }
    } else {
      // Collapsing the rail hides the workspace, so strip the deep-
      // link params that point into it; otherwise the URL advertises
      // a workspace view a reload would re-open.
      clearFileViewerUrl();
    }
    setRightPanelOpen(next);
  };

  // The hotkey (⌘⌥[) and command-palette toggle for the left sidebar. A peeking
  // sidebar counts as open, so toggling collapses it; either way peek is
  // cleared so we never leave `sidebarOpen` and `sidebarPeek` both true (a
  // floating-card layout the rest of the shell treats as a pushing panel).
  const toggleLeftSidebar = () => {
    // On /settings the sidebar holds the only exit (the Back row), so collapsing
    // it — by hotkey or command palette, the paths that bypass the hidden
    // title-bar toggle — would strand the user on the page. Opening is still
    // fine; only the collapse direction is refused. The pre-settings state stays
    // stashed either way, so what the user had before the visit is still what
    // gets restored on the way out.
    if (inSettings) {
      setSidebarOpen(true);
      setSidebarPeek(false);
      return;
    }
    setSidebarOpen(!(sidebarOpen || sidebarPeek));
    setSidebarPeek(false);
  };

  // Dwell-to-peek for the macOS title-bar toggle. ChatHeader owns this for its
  // own collapsed-state button, but that button is hidden on the mac shell (the
  // title-bar cluster replaces it), so the behaviour has to exist here too or
  // dwell-to-peek would only work on a button the user can no longer see.
  // Same 400ms as ChatHeader: a quick pass-over must not open the card.
  const titleBarPeekTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const titleBarPeekRequest = useRef(0);
  const cancelTitleBarPeek = useCallback(() => {
    titleBarPeekRequest.current += 1;
    if (titleBarPeekTimer.current) {
      clearTimeout(titleBarPeekTimer.current);
      titleBarPeekTimer.current = null;
    }
  }, []);
  const armTitleBarPeek = useCallback(() => {
    cancelTitleBarPeek();
    const request = titleBarPeekRequest.current;
    titleBarPeekTimer.current = setTimeout(() => {
      titleBarPeekTimer.current = null;
      if (titleBarPeekRequest.current !== request) return;
      setSidebarPeek(true);
      setSidebarOpen(false);
    }, 400);
  }, [cancelTitleBarPeek]);
  useEffect(() => cancelTitleBarPeek, [cancelTitleBarPeek]);

  // Dismiss a peeking card when the pointer is clearly elsewhere.
  //
  // The card closes itself on its own pointerleave, which is enough when peek is
  // armed from a button INSIDE it. The title-bar trigger sits outside, so a
  // pointer that dwells there and then moves away without ever crossing the card
  // leaves it with no pointerenter — and therefore no pointerleave — so nothing
  // closes it and the card sits open indefinitely. Watch the document instead:
  // once the pointer is over neither the card nor the trigger, dismiss on the
  // same 200ms grace the card uses, so a wobble between the two doesn't.
  //
  // A click outside dismisses immediately: at that point the user has committed
  // their attention elsewhere and waiting out a grace period just feels sticky.
  const peekDismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!sidebarPeek) return;
    const cancel = () => {
      if (peekDismissTimer.current) {
        clearTimeout(peekDismissTimer.current);
        peekDismissTimer.current = null;
      }
    };
    // Anything the peek card legitimately spawns outside its own subtree (Radix
    // menus, tooltips, dialogs) must not count as "outside", or opening a row's
    // context menu would dismiss the card under it.
    const insidePeekSurface = (target: EventTarget | null) => {
      if (!(target instanceof Element)) return false;
      return !!target.closest(
        [
          "aside.conversations-sidebar",
          ".electron-sidebar-header-actions",
          "[data-radix-popper-content-wrapper]",
          '[role="menu"]',
          '[role="dialog"]',
          '[role="tooltip"]',
        ].join(","),
      );
    };
    const onPointerMove = (e: PointerEvent) => {
      if (insidePeekSurface(e.target)) {
        cancel();
        return;
      }
      if (peekDismissTimer.current) return;
      peekDismissTimer.current = setTimeout(() => {
        peekDismissTimer.current = null;
        setSidebarPeek(false);
      }, 200);
    };
    const onPointerDown = (e: PointerEvent) => {
      if (insidePeekSurface(e.target)) return;
      cancel();
      setSidebarPeek(false);
    };
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => {
      cancel();
      document.removeEventListener("pointermove", onPointerMove);
      document.removeEventListener("pointerdown", onPointerDown, true);
    };
  }, [sidebarPeek]);

  // Toggle the workspace rail's full-screen (maximized) state. Entering
  // collapses the left sidebar (the maximized rail wants the full width) after
  // stashing its prior open-state; exiting restores that state. The sidebar
  // side effect is done here — outside any state updater — so the maximize
  // setter stays a plain boolean flip. ``restoreSidebarAfterMaximize`` is
  // shared with the session-switch reset, which also drops out of full screen.
  const restoreSidebarAfterMaximize = useCallback(() => {
    setSidebarOpen(sidebarOpenBeforeMaximizeRef.current);
  }, []);
  const toggleRightPanelMaximized = () => {
    if (!rightPanelMaximized) {
      sidebarOpenBeforeMaximizeRef.current = sidebarOpen;
      setSidebarOpen(false);
    } else {
      restoreSidebarAfterMaximize();
    }
    setRightPanelMaximized((prev) => !prev);
  };

  // ⌘⌥[ / ⌘⌥] (Ctrl+Alt on Win/Linux) toggle the left and right sidebars. Bound
  // here where both panels' open-state lives.
  useSidebarToggleHotkeys({
    onToggleLeft: toggleLeftSidebar,
    onToggleRight: toggleRightPanel,
  });

  // ⌘K (Ctrl+K) toggles the command palette. Bound capture-phase, so in the
  // embedded build we claim the chord ahead of any host-page ⌘K listener.
  // Bound here where the palette's open-state lives.
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const isEmbedded = useIsEmbedded();
  useCommandPaletteHotkey(() => setCommandPaletteOpen((prev) => !prev));
  useNewSessionHotkey(!isEmbedded);

  // Mobile back button: close the open file and return to the files/changes
  // list. On mobile the tab strip is hidden, so a "back" should fully drop the
  // file (remove it from openFiles) rather than leaving an orphan tab the user
  // can't see — making the tabs feature effectively invisible on small screens.
  // The mobile viewer is a full-screen overlay above the chat, so we also open
  // the files drawer; otherwise closing would reveal the chat, not the list.
  function closeFileViewer() {
    setOpenFiles((prev) => prev.filter((p) => p !== selectedFilePath));
    setSelectedFilePath(null);
    setFileViewerCommentsOpen(false);
    clearFileViewerUrl();
    setFilesPanelOpen(true);
  }

  // Deselect the active file tab to reveal the scope view (Changed/All list
  // or tree). Keeps ``openFiles`` intact — the tabs stay available; only the
  // active selection clears. Invoked when the user clicks a scope button.
  //
  // Depends on ``clearFileViewerUrl`` (NOT ``[]``): AppShell is a layout route
  // that never remounts across ``/c/:a`` → ``/c/:b``, so a ``[]``-frozen
  // callback would keep calling react-router's first-render ``navigate``, whose
  // relative ``setSearchParams`` resolves against the pathname captured at
  // first mount. Clicking a scope button would then strip the file params *and*
  // yank the URL back to whichever conversation was open when AppShell mounted.
  // Tracking ``clearFileViewerUrl`` (→ ``setSearchParams`` → ``navigate`` →
  // ``locationPathname``) keeps it bound to the conversation in view now.
  const showScopeView = useCallback(() => {
    setSelectedFilePath((active) => {
      if (active === null) return active;
      setFileViewerCommentsOpen(false);
      clearFileViewerUrl();
      return null;
    });
  }, [clearFileViewerUrl]);

  // Close a single file tab. If the closed tab was the active one, activate
  // its neighbor (prefer the previous tab, else the next); when no tabs
  // remain, fall back to a scope view (clearing ?file=). Closing a
  // non-active tab leaves the active selection untouched.
  const closeFile = useCallback(
    (path: string) => {
      setOpenFiles((prev) => {
        const idx = prev.indexOf(path);
        if (idx === -1) return prev;
        const next = prev.filter((p) => p !== path);
        setSelectedFilePath((active) => {
          if (active !== path) return active;
          if (next.length === 0) {
            setFileViewerCommentsOpen(false);
            clearFileViewerUrl();
            return null;
          }
          const neighbor = next[idx - 1] ?? next[idx] ?? next[0];
          setSearchParams(
            (sp) => {
              const params = new URLSearchParams(sp);
              params.set("file", neighbor);
              params.delete("comment");
              return params;
            },
            { replace: true },
          );
          return neighbor;
        });
        return next;
      });
    },
    [clearFileViewerUrl, setSearchParams],
  );

  // Switch the workspace rail's tab. The side effect (closing any open
  // file + its comments + URL) lives here, not in WorkspacePanel, so the
  // tab state and the file state can't drift apart — the rail stays a
  // dumb view. The Files/Changes tabs own the viewer, so there's no
  // per-tab file to stash and restore; switching tabs just closes any
  // open file to reveal the picked tab's scope list.
  function handleRightRailTabChange(next: RightRailTab) {
    setRightRailTab(next);
    if (selectedFilePath !== null) {
      setSelectedFilePath(null);
      setFileViewerCommentsOpen(false);
      clearFileViewerUrl();
    }
    // Clicking a static nav tab deselects any active shell tab (it stays in
    // the strip) so the picked tab's content shows in the single slot.
    if (selectedTerminalKey !== null) {
      setSelectedTerminalKey(null);
    }
  }

  function openTerminalsPanel(key: string) {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setExecutionLogsKey(null); // close execution-logs panel
    setFilesPanelOpen(false); // close files drawer
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setShellsPanelOpen(false); // close mobile shells drawer
    setPanelInitialKey(key);
  }

  // Open a shell as a tab in the desktop workspace rail (the mobile path
  // still uses ``openTerminalsPanel`` → full-screen drawer). Adds the key to
  // the open-tabs strip if new, activates it, and reveals the rail. Clearing
  // the selected file keeps the rail's single content slot unambiguous — a
  // shell tab and a file tab can't both be "active".
  const openTerminalTab = useCallback(
    (key: string) => {
      // The tab already exists (tabs derive 1:1 from the terminal list); this
      // just focuses it and reveals the rail. The selection is sticky (never
      // pruned off the list), so a fresh create that's still landing stays
      // selected and its xterm surfaces the instant the terminal appears.
      autoFocusTerminalKeyRef.current = key;
      setSelectedTerminalKey(key);
      setSelectedFilePath(null);
      setFileViewerCommentsOpen(false);
      clearFileViewerUrl();
      setExecutionLogsKey(null);
      setFilesPanelOpen(false);
      setSubagentsPanelOpen(false);
      setShellsPanelOpen(false);
      setRightPanelOpen(true);
      if (conversationId) writeSessionWorkspaceState(conversationId, { open: true });
    },
    [clearFileViewerUrl, conversationId],
  );

  // Focus a shell the user just created ("+"→Shell) as soon as its tab appears
  // — a new non-agent terminal key that wasn't present when the create started.
  // This runs off the live list, so it fires on the reconcile poll / SSE even
  // before the create POST resolves (which lags on a waking runner), so the
  // opened shell doesn't sit unselected while its tab is already showing.
  useEffect(() => {
    const known = pendingShellCreateRef.current;
    if (known === null) return;
    const fresh = openTerminals.find((k) => !known.has(k) && !isAgentTerminalKey(k));
    if (fresh !== undefined) {
      pendingShellCreateRef.current = null;
      openTerminalTab(fresh);
    }
  }, [openTerminals, openTerminalTab]);

  // User clicked a tab's "x". Closing a tab kills the underlying terminal, so
  // confirm first rather than acting immediately.
  const requestCloseTerminal = useCallback((key: string) => {
    setTerminalPendingClose(key);
  }, []);
  // Confirmed close: kill the terminal (DELETE). The `session.resource.deleted`
  // delta + the mutation's cache prune drop it from the list, so its tab
  // disappears. If it was the active tab, move focus to a neighbor (prefer the
  // previous, else the next) so the rail doesn't snap back to Files; when none
  // remain the selection clears.
  const confirmCloseTerminal = useCallback(() => {
    const key = terminalPendingClose;
    if (key === null) return;
    setTerminalPendingClose(null);
    setSelectedTerminalKey((active) => {
      if (active !== key) return active;
      const idx = openTerminals.indexOf(key);
      const remaining = openTerminals.filter((k) => k !== key);
      if (remaining.length === 0) return null;
      return remaining[idx - 1] ?? remaining[idx] ?? remaining[0];
    });
    const info = terminals.find((t) => terminalTabKey(t) === key);
    if (info && conversationId) deleteTerminalMutation.mutate(info.id);
  }, [terminalPendingClose, openTerminals, terminals, conversationId, deleteTerminalMutation]);

  // The active shell selection is sticky: it changes only on explicit user
  // action (opening a shell/file, switching a nav tab, closing the active
  // shell). We deliberately do NOT clear it off the terminal list — the list
  // churns transiently (runner health-poll clearing the cache, create/SSE
  // races), and pruning against those windows was stranding a just-opened shell
  // unselected once its tab recovered. Instead the rail only *shows* the
  // selected shell's xterm while its terminal is actually present
  // (``WorkspacePanel`` gates on ``openTerminals``); a selection whose terminal
  // is momentarily or permanently absent falls back to the default view, and
  // reappears the instant the terminal returns.

  function openExecutionLogsPanel(key: string) {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setFilesPanelOpen(false); // close files drawer
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setShellsPanelOpen(false); // close mobile shells drawer
    setExecutionLogsKey(key);
  }

  // Mobile FAB → "Files" / "Changes" both open the files drawer (mirroring the
  // desktop rail's two tabs); the entry picks the scope the drawer opens in.
  function openFilesDrawer(flatView: boolean) {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setExecutionLogsKey(null); // close execution-logs panel
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setShellsPanelOpen(false); // close mobile shells drawer
    setFilesDrawerFlatView(flatView);
    setFilesPanelOpen(true);
  }
  const openFilesPanel = () => openFilesDrawer(false);
  const openChangesPanel = () => openFilesDrawer(true);

  // Mobile FAB → "Agents" opens the subagents list (the desktop rail's
  // Agents tab) as a full-screen drawer.
  function openSubagentsPanel() {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setExecutionLogsKey(null); // close execution-logs panel
    setFilesPanelOpen(false); // close files drawer
    setShellsPanelOpen(false); // close mobile shells drawer
    setSubagentsPanelOpen(true);
  }

  // Mobile FAB → "Shells" opens the session's shells list as a full-screen
  // drawer (there is no desktop Shells tab; desktop opens shells as soft
  // tabs). This preserves the "+ New shell" empty state on phones instead
  // of requiring an existing shell before the entry works.
  function openShellsPanel() {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel / terminal-first view
    setExecutionLogsKey(null); // close execution-logs panel
    setFilesPanelOpen(false); // close files drawer
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setShellsPanelOpen(true);
  }

  function openMainExecutionLog() {
    // Mobile FAB → "Execution logs" jumps straight to the main thread.
    // Children are reachable via the panel's tab switcher.
    openExecutionLogsPanel(executionLogTabKey(MAIN_EXECUTION_LOG_KEY));
  }

  // Workspace root + runner home let chat path-links collapse absolute or
  // ``~``-relative paths the agent mentions onto workspace-relative ones.
  // Sourced from the same environment query that gates the files panel.
  const workspaceRoot = environmentQuery.data?.root ?? null;
  const workspaceHome = environmentQuery.data?.home ?? null;
  const fileViewerContextValue = useMemo(
    () => ({
      openFile: openFileViewer,
      isChangedPath,
      conversationId,
      workspaceRoot,
      workspaceHome,
    }),
    [openFileViewer, isChangedPath, conversationId, workspaceRoot, workspaceHome],
  );

  // Context for descendants — ChatPage's ConnectionIndicator reads
  // this to render the inline Chat/Terminal segmented pill. `setView`
  // routes through the same `setPanelInitialKey` setter as the rail,
  // so all surfaces share one source of truth.
  const setView = useCallback(
    (view: "chat" | "terminal") => {
      if (view === "chat") {
        setPanelInitialKey(null);
        return;
      }
      if (agentTerminal === null) {
        if (terminalFirst) setPanelInitialKey(PANEL_NO_TERMINAL_KEY);
        return;
      }
      // The pill targets only the agent's REPL/vendor pane. A user shell may
      // be the only cached terminal briefly, but it belongs in the workspace
      // rail and must never become the main Terminal view.
      setPanelInitialKey(terminalTabKey(agentTerminal));
    },
    [terminalFirst, agentTerminal, setPanelInitialKey],
  );

  // Shells do not make the Terminal-view toggle available: that control owns
  // only the session's agent REPL/vendor pane.
  const terminalsAvailable = agentTerminal !== null;
  // Single pill-facing "loading" signal: the PTY is not ready, but is coming up —
  // either the runner is launching/relaunching (liveness `starting`, known the
  // instant a message is sent) or it's up and auto-creating the PTY
  // (`terminalPending`). Idle stopped sessions are neither → no spinner.
  // Suppressed once the session has failed AND no send is in flight: a runner
  // that crashed before connecting (`runner_failed_to_start`), or a host that
  // refused the launch (`harness_not_configured`), sits in the `starting` grace
  // window but can never come up — so drop the spinner and let the error banner
  // stand alone. A runner *disconnect* also marks the session failed, though,
  // and that status lingers until the relaunched runner pushes a fresh edge —
  // so an in-flight send (`chatStatus === "streaming"`, which is what upgrades
  // liveness to `starting`) overrides the suppression: the host is relaunching
  // the runner right now and the user must see the spinner, not a silent gap.
  // If the relaunch fails, the fresh `session.status: failed` edge settles the
  // local send lifecycle back to `idle` and the suppression re-engages.
  // `sessionStatus` (declared above) is set by both the live
  // `session.status:failed` push and the snapshot reload.
  // A host switch tears down the old runner, which can push a lingering
  // `failed` before the new one connects — so a just-requested launch lifts
  // the failed-suppression exactly like an in-flight send does.
  const launchPending =
    runnerLaunchedAt !== null && Date.now() - runnerLaunchedAt < STARTING_GRACE_S * 1000;
  // Until the liveness row or session snapshot hydrates, a brand-new
  // session is indistinguishable from a stopped one — treat the unobserved
  // window as starting so the stopped UI can't flash during startup.
  const livenessRowPending =
    activeConv === null && activeSession === null && (conversationsLoading || sessionLoading);
  // The runner can register online before its auto-created PTY reaches the
  // inventory; hold startup through that gap inside the cold-boot grace. A
  // PTY seen for THIS conversation and then removed is a stop, not a gap.
  const createdAtS = livenessRow?.created_at;
  const hadPtyForConvRef = useRef<string | null>(null);
  useEffect(() => {
    if (agentTerminal) hadPtyForConvRef.current = conversationId ?? null;
  }, [conversationId, agentTerminal]);
  const freshOnlineGrace =
    terminalFirst &&
    liveness.kind === "online" &&
    hadPtyForConvRef.current !== conversationId &&
    typeof createdAtS === "number" &&
    createdAtS > 0 &&
    Date.now() / 1000 - createdAtS < STARTING_GRACE_S;
  const terminalStartingUp =
    !terminalsAvailable &&
    (sessionStatus !== "failed" || chatStatus === "streaming" || launchPending) &&
    (livenessRowPending ||
      launchPending ||
      // terminal_pending is the runner's own "creating the PTY" signal,
      // accurate in the snapshot after a reload; gate on an alive-ish
      // liveness so a stale flag from a dead runner can't pin the spinner.
      ((terminalPending || activeSession?.terminalPending === true) &&
        (liveness.kind === "online" ||
          liveness.kind === "starting" ||
          liveness.kind === "unknown")) ||
      // A send in flight is this client (re)launching the runner now.
      (chatStatus === "streaming" && liveness.kind !== "online") ||
      freshOnlineGrace);
  // A rail-opened shell (any open terminal key other than the agent's
  // own terminal) takes over the main view chrome-free:
  // ConnectionIndicator hides the Chat/Terminal pill while this is
  // true, and MainTerminalView renders the shell with its own close
  // affordance. The PANEL_NO_TERMINAL_KEY sentinel ("") is falsy, so
  // "open with no target" stays a pill view.
  const isShellView = terminalFirst && !!panelInitialKey && !isAgentTerminalKey(panelInitialKey);
  const shellViewTargetAvailable =
    isShellView && terminals.some((terminal) => terminalTabKey(terminal) === panelInitialKey);

  // An explicit user shell has no useful empty state: if it disappears, return
  // to chat. The agent-terminal view is different — it remains selectable when
  // the runner is offline and owns the resume affordance shown in that state.
  // Edge-triggered + startingUp-guarded so a shell relaunch isn't yanked.
  const hadShellTerminalRef = useRef(false);
  useEffect(() => {
    if (
      terminalFirst &&
      panelOpen &&
      isShellView &&
      hadShellTerminalRef.current &&
      !shellViewTargetAvailable &&
      !terminalStartingUp
    ) {
      setPanelInitialKey(null);
    }
    hadShellTerminalRef.current = shellViewTargetAvailable;
  }, [
    terminalFirst,
    panelOpen,
    isShellView,
    shellViewTargetAvailable,
    terminalStartingUp,
    setPanelInitialKey,
  ]);

  const terminalFirstContextValue = useMemo<TerminalFirstContextValue>(
    () => ({
      isClaudeNative,
      isNativeWrapper,
      isTerminalFirst: terminalFirst,
      isShellView,
      view: panelOpen ? "terminal" : "chat",
      terminalViewKey: panelInitialKey,
      setView,
      terminalsAvailable,
      terminalStartingUp,
    }),
    [
      isClaudeNative,
      isNativeWrapper,
      terminalFirst,
      isShellView,
      panelOpen,
      panelInitialKey,
      setView,
      terminalsAvailable,
      terminalStartingUp,
    ],
  );

  // Opener for the fork/clone dialog, shared with descendants via
  // ForkDialogContext. ChatPage's per-message "Fork from here" action is
  // the only fork entry point (no header/menu Clone button).
  const forkDialogContextValue = useMemo<ForkDialogContextValue>(
    () => ({
      canFork: canClone,
      openForkDialog: (opts?: { upToResponseId?: string }) => {
        setForkUpToResponseId(opts?.upToResponseId ?? null);
        setForkOpen(true);
      },
    }),
    [canClone],
  );
  const workspacePanelVisible = Boolean(
    conversationId &&
    hasRailContent &&
    rightPanelOpen &&
    (terminalFirst || !panelOpen) &&
    !executionLogsOpen &&
    !filesPanelOpen,
  );

  return (
    <FileViewerContext.Provider value={fileViewerContextValue}>
      <TerminalFirstContextProvider value={terminalFirstContextValue}>
        <ForkDialogContextProvider value={forkDialogContextValue}>
          {/* `app-shell` paints the near-white brand gradient canvas (see
        index.css); bg-sidebar is the fallback the gradient sits over. The
        white sidebar / workspace cards float on this canvas.

        data-electron-mac scopes the frameless-window CSS in index.css: the
        macOS Electron shell hides the native title bar (titleBarStyle
        "hiddenInset"), so the web layer drops the sidebar below the
        traffic lights and supplies a drag strip in the freed space. */}
          <div
            className="app-shell relative flex h-dvh bg-sidebar text-foreground"
            // Reflect the docked sidebar's open state so CSS can drop the
            // traffic-light clearance on surfaces the sidebar covers (see the
            // maximized workspace rail's tab strip in index.css): with the
            // sidebar open over the window corner there are no lights to clear.
            data-sidebar-open={sidebarOpen ? "true" : undefined}
            data-electron-mac={isMacElectronShell() ? "true" : undefined}
            data-ios-native={isIOSShell() ? "true" : undefined}
            data-android-native={isAndroidShell() ? "true" : undefined}
          >
            {/* Frameless-window titlebar stand-in (macOS Electron only): the
          sidebar's electron top margin (see index.css) frees this strip of
          canvas for the traffic lights, and the strip is the window's one
          drag surface — content below and right stays fully clickable. */}
            {isMacElectronShell() && <div className="electron-drag-strip" aria-hidden="true" />}
            {/* The Search/Settings/toggle cluster lives HERE on the macOS shell,
          not in the sidebar, so the three icons hold one fixed position beside
          the traffic lights no matter what the sidebar does. Inside the sidebar
          they would be clipped and inert once it collapses (md:w-0 +
          overflow-hidden + inert), and while peeking the floating card's
          inset-2 would drag them off the lights' centre line. The sidebar's own
          copy is hidden on mac by CSS; this one is positioned by
          .electron-sidebar-header-actions in index.css. */}
            {/* Hidden on /settings: the settings nav replaces the session list
          INSIDE the sidebar, and its "Back" row is the only way out. Leaving a
          collapse toggle up here would let the user hide the one exit and strand
          themselves on the settings page (the row exists but is clipped and
          inert). So on /settings the sidebar is pinned open — see
          forceSidebarOpenInSettings — and these controls step aside rather than
          offer an action that would break the page. */}
            {isMacElectronShell() && !inSettings && (
              <div className="electron-sidebar-header-actions">
                <SidebarHeaderActions
                  // Real docked state — NOT `|| sidebarPeek`. During a peek the
                  // sidebar isn't pinned open, and `onToggle` below pins it open
                  // (the else branch, since sidebarOpen is false), so the button
                  // must read "Open" and expand. Counting peek as expanded made
                  // it show "Collapse" while a click actually expanded — the
                  // icon/tooltip lied until the sidebar was really open.
                  expanded={sidebarOpen}
                  onToggle={() => {
                    cancelTitleBarPeek();
                    // Mirrors the ⌘⌥[ hotkey: a peeking sidebar counts as open,
                    // so toggling from peek pins it open rather than collapsing.
                    if (sidebarOpen) {
                      setSidebarOpen(false);
                    } else {
                      setSidebarOpen(true);
                    }
                    setSidebarPeek(false);
                  }}
                  onOpenSearch={() => setCommandPaletteOpen(true)}
                  // Dwell-to-peek moves here with the button: on mac this cluster
                  // replaces ChatHeader's collapsed-state toggle, which is where
                  // peek was armed, so without this the affordance would vanish.
                  // Only meaningful while collapsed — peeking or open, there is
                  // nothing to peek at.
                  onTogglePointerEnter={sidebarOpen || sidebarPeek ? undefined : armTitleBarPeek}
                  onTogglePointerDown={cancelTitleBarPeek}
                  onTogglePointerLeave={cancelTitleBarPeek}
                />
              </div>
            )}
            {/* The server picker is NOT here: it lives at the bottom of the
          sidebar (SidebarServerPicker). This strip is shared with the chat
          header — which is taller and also anchored at top-0 — so a centered
          "<thread> — <host>" label here collided with the header's action
          cluster on a narrow window. The strip stays pure drag surface. */}
            <Sidebar
              open={sidebarOpen}
              onOpen={() => {
                setSidebarOpen(true);
                setSidebarPeek(false);
              }}
              peek={sidebarPeek}
              dragProgress={sidebarDragProgress}
              onClose={() => {
                setSidebarOpen(false);
                setSidebarPeek(false);
              }}
              onOpenSearch={() => setCommandPaletteOpen(true)}
            />

            {/* Content region (everything right of the sidebar): a relative
          flex row holding the chat+workspace group and the push panels
          as siblings. */}
            <div className="relative flex min-h-0 min-w-0 flex-1">
              {/* Chat + workspace group. The full-width header overlay is
            scoped to this group, so it spans the chat *and* the right
            workspace card but never reaches over the push panels (which
            render their own top chrome as siblings outside the group).
            Hidden on desktop when the TerminalsPanel push panel takes
            over — *except* in terminal-first sessions, where the terminal
            renders inline in main (via MainTerminalView) and the
            workspace card stays visible alongside. */}
              <div
                className={cn(
                  "relative flex min-h-0 min-w-0 flex-1",
                  panelOpen && !terminalFirst && "md:hidden",
                )}
                style={
                  {
                    "--workspace-panel-offset": workspacePanelVisible
                      ? `${inlinePanelWidth}px`
                      : "0px",
                  } as CSSProperties
                }
              >
                <ChatHeader
                  // Real docked state — deliberately NOT `|| sidebarPeek`. Peek
                  // is a transient card floating over the collapsed layout (the
                  // docked sidebar stays w-0), so the header must keep its
                  // collapsed left slot. Treating peek as open relaid it out —
                  // the toggle unmounted and the breadcrumb slid left into its
                  // spot — shifting the title sideways the instant the peek card
                  // appeared. Left collapsed, the breadcrumb stays put beneath
                  // the floating card (and in the title-bar strip on mac).
                  sidebarOpen={sidebarOpen}
                  onOpenSidebar={(peek?: boolean) => {
                    if (peek) {
                      setSidebarPeek(true);
                      setSidebarOpen(false);
                    } else {
                      setSidebarOpen(true);
                      setSidebarPeek(false);
                    }
                  }}
                  isChildSession={isChildSession}
                  conversationId={conversationId}
                  actionConversation={actionConversation}
                  conversationTitle={headerConversationTitle}
                  projectName={headerProjectName}
                  titleLinkTo={headerTitleLinkTo}
                  boundAgent={boundAgent}
                  wrapperLabel={wrapperLabel}
                  canShare={canShare}
                  shareDisabled={shareDisabled}
                  shareDisabledReason={shareDisabledReason}
                  onShare={() => setShareOpen(true)}
                  hasAgentInfo={hasAgentInfo}
                  onAgentInfo={() => setAgentInfoOpen(true)}
                  hasHeaderMenu={hasHeaderMenu}
                  showFilesPanel={showFilesPanel}
                  hasRailContent={hasRailContent}
                  rightPanelOpen={rightPanelOpen}
                  onToggleRightPanel={toggleRightPanel}
                  mobileMenu={{
                    fileViewerOpen,
                    panelOpen,
                    terminalFirst,
                    executionLogsOpen,
                    filesPanelOpen,
                    subagentsPanelOpen,
                    shellsPanelOpen,
                    hideTerminalsTab,
                    // Mobile: reachable when a shell exists OR the agent
                    // declares shell access (so the drawer's "+ New shell" row
                    // can create the first one). Desktop rail tab stays gated
                    // on an existing shell (railTabsAvailable.terminals).
                    showShellsTab:
                      !hideTerminalsTab && (railTerminals.length > 0 || agentSupportsShells),
                    terminalsLength: railTerminals.length,
                    debugMode,
                    changedCount,
                    subagentsWorking,
                    agentCount,
                    onOpenFiles: openFilesPanel,
                    onOpenChanges: openChangesPanel,
                    onOpenShells: openShellsPanel,
                    onOpenSubagents: openSubagentsPanel,
                    onOpenMainExecutionLog: openMainExecutionLog,
                  }}
                />
                <main className="relative flex min-h-0 min-w-0 flex-1 flex-col">
                  <Outlet />
                </main>

                {/* Debug-mode execution-logs rail — desktop only, hidden when a
              push panel is open (the panel itself becomes the focus). Only
              rendered in debug mode so the column doesn't occupy space in
              normal use. */}
                {conversationId && debugMode && !panelOpen && !executionLogsOpen && (
                  <div className="hidden md:flex md:flex-col md:w-56 md:shrink-0 md:border-l md:border-border md:overflow-y-auto md:px-2 md:pb-2 md:pt-12 md:gap-2">
                    <SessionRail
                      conversationId={conversationId}
                      onExpandExecutionLogs={openExecutionLogsPanel}
                      suppressed={false}
                    />
                  </div>
                )}

                {/* Right workspace card — gated on conversationId (panels have
              no workspace to read without a session), default-open,
              hidden when any push panel takes the right side, *except* in
              terminal-first sessions where the terminal renders inline
              (not as a push panel) and the card should remain visible
              alongside it. Also gated on the rail having at least one
              available tab — otherwise the card renders as an empty white
              rectangle (e.g. a no-filesystem agent with no terminals).
              Sits inside the group so the header overlay spans it; the
              push panels below sit outside the group. */}
                {conversationId && workspacePanelVisible && (
                  <WorkspacePanel
                    conversationId={conversationId}
                    width={inlinePanelWidth}
                    inert={inlinePanelWidth === 0}
                    handleProps={inlinePanelHandleProps}
                    rightRailTab={rightRailTab}
                    onRightRailTabChange={handleRightRailTabChange}
                    showFilesPanel={showFilesPanel}
                    showBrowserTab={railTabsAvailable.browser}
                    changedCount={changedCount}
                    subagentsWorking={subagentsWorking}
                    agentCount={agentCount}
                    rootSessionId={rootSessionId}
                    selectedFilePath={selectedFilePath}
                    openFiles={openFiles}
                    openFileViewer={openFileViewer}
                    onCloseFile={closeFile}
                    onShowScopeView={showScopeView}
                    onCommentsOpenChange={setFileViewerCommentsOpen}
                    openTerminalTab={openTerminalTab}
                    openTerminals={openTerminals}
                    selectedTerminalKey={selectedTerminalKey}
                    autoFocusSelectedTerminal={
                      autoFocusTerminalKeyRef.current !== null &&
                      autoFocusTerminalKeyRef.current === selectedTerminalKey
                    }
                    closingTerminalKey={closingTerminalKey}
                    onCloseTerminal={requestCloseTerminal}
                    maximized={rightPanelMaximized}
                    onToggleMaximized={toggleRightPanelMaximized}
                    permissionLevel={permissionLevel}
                    filesPanelSort={filesPanelSort}
                    onSortChange={handleFilesSortChange}
                    filesPanelShowHidden={filesPanelShowHidden}
                    onShowHiddenChange={setFilesPanelShowHidden}
                    liveness={liveness}
                    onShellCreateStart={markShellCreateStarted}
                    onShellCreateFailed={clearShellCreatePending}
                  />
                )}
              </div>

              {/* Push panels — flex siblings to main, animate width. Only one is open at a time.
          Terminal-first sessions render the terminal inline inside main
          (via MainTerminalView in ChatPage) and never mount the drawer. */}
              {conversationId && !terminalFirst && (
                <TerminalsPanel
                  open={panelOpen}
                  conversationId={conversationId}
                  initialTerminalKey={panelInitialKey}
                  // No neighbor to resize against (chat is hidden, FilesPanel
                  // owns its own width) — grow via flex-1.
                  fluid={panelOpen}
                  // Non-owners attach read-only: a shared PTY can't attribute
                  // input per-user, so only the owner may type (server-enforced).
                  readOnly={!isOwnerLevel(permissionLevel)}
                  onClose={() => setPanelInitialKey(null)}
                />
              )}
              {conversationId && (
                <ExecutionLogsPanel
                  open={executionLogsOpen}
                  conversationId={conversationId}
                  initialKey={executionLogsKey}
                  onClose={() => setExecutionLogsKey(null)}
                />
              )}
              {conversationId && showFilesPanel && (
                <FilesPanelDrawer
                  open={filesPanelOpen}
                  onClose={() => setFilesPanelOpen(false)}
                  onFileSelect={openFileViewer}
                  flatView={filesDrawerFlatView}
                  showHidden={filesPanelShowHidden}
                  onShowHiddenChange={setFilesPanelShowHidden}
                  sort={filesPanelSort}
                  onSortChange={handleFilesSortChange}
                />
              )}
              {/* Mobile-only full-screen drawers for the rail tabs that have no
          desktop push panel of their own. `MobilePanelDrawer` is `md:hidden`,
          so these never collide with the desktop rail; they're opened from
          the session-menu FAB above. */}
              {conversationId && rootSessionId && (
                <MobilePanelDrawer
                  open={subagentsPanelOpen}
                  title="Agents"
                  onClose={() => setSubagentsPanelOpen(false)}
                  testId="subagents-panel-drawer"
                >
                  <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
                </MobilePanelDrawer>
              )}
              {conversationId && (
                <MobilePanelDrawer
                  open={shellsPanelOpen}
                  title="Shells"
                  onClose={() => setShellsPanelOpen(false)}
                  testId="shells-panel-drawer"
                >
                  <InlineTerminalsSection
                    conversationId={conversationId}
                    onExpand={openTerminalsPanel}
                    // Mobile has no tab strip "+" menu, so the drawer carries
                    // the "+ New shell" create row.
                    showNewShell
                  />
                </MobilePanelDrawer>
              )}
              {/* Mobile-only push panel — on desktop the viewer lives inside the inline aside. */}
              {conversationId && selectedFilePath !== null && (
                <div className="md:hidden">
                  <FileViewer
                    open
                    conversationId={conversationId}
                    path={selectedFilePath}
                    onClose={closeFileViewer}
                    onNavigateTo={openFileViewer}
                    permissionLevel={permissionLevel}
                    sort={filesPanelSort}
                  />
                </div>
              )}
            </div>
          </div>
          {conversationId && (
            <PermissionsModal
              sessionId={conversationId}
              open={shareOpen}
              onOpenChange={setShareOpen}
            />
          )}
          {conversationId && (
            <ForkSessionDialog
              // Remount per session so the title prefill (captured at mount)
              // re-derives when the user navigates between sessions.
              key={`fork-session-dialog-${conversationId}`}
              sourceSessionId={conversationId}
              sourceTitle={activeSession?.title}
              sourceWorkspace={activeSession?.workspace ?? parentConv?.workspace}
              sourceHostId={activeSession?.hostId ?? parentConv?.host_id}
              sourceGitBranch={activeSession?.gitBranch}
              upToResponseId={forkUpToResponseId}
              open={forkOpen}
              onOpenChange={(open) => {
                setForkOpen(open);
                // Closing clears the truncation point so a later Clone (or
                // reopened dialog) doesn't silently fork a partial history.
                if (!open) setForkUpToResponseId(null);
              }}
            />
          )}
          <CloseShellDialog
            open={terminalPendingClose !== null}
            shellLabel={(() => {
              if (terminalPendingClose === null) return null;
              const info = terminals.find((t) => terminalTabKey(t) === terminalPendingClose);
              if (!info) return null;
              return info.session ? `${info.name} · ${info.session}` : info.name;
            })()}
            onConfirm={confirmCloseTerminal}
            onCancel={() => setTerminalPendingClose(null)}
          />
          {/* Agent tools & policies — the mobile counterpart of the desktop
        AgentInfoButton popover, opened from the header's three-dot menu. */}
          {hasAgentInfo && (
            <Dialog open={agentInfoOpen} onOpenChange={setAgentInfoOpen}>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Agent</DialogTitle>
                  <DialogDescription className="sr-only">
                    Tools and policies configured for the active agent.
                  </DialogDescription>
                </DialogHeader>
                <AgentInfoContent agent={boundAgent} sessionId={conversationId} />
              </DialogContent>
            </Dialog>
          )}
          {/* Keyboard-shortcuts reference. Self-contained (owns its open state +
              ⌘/Ctrl+/ opener); ungated so it works on every route. */}
          <KeyboardShortcutsDialog />
          {/* Global command palette (⌘K). Ungated so it works on every route
              and in embedded mode — the sidebar's "Search" button opens it
              there even though the ⌘K hotkey is disabled (it belongs to the
              host page). */}
          <CommandPalette
            open={commandPaletteOpen}
            onOpenChange={setCommandPaletteOpen}
            onToggleLeftSidebar={toggleLeftSidebar}
            onToggleRightSidebar={toggleRightPanel}
          />
          {/* Transient toasts (e.g. "session archived"). Mounted once here so
              any surface can fire one via showToast(). */}
          {/* Match the previous toast system's effectively unbounded stack so
              security prompts cannot be hidden behind ordinary notifications. */}
          <Toaster
            position="bottom-right"
            visibleToasts={100}
            offset={{
              right: "1rem",
              bottom: updateOverlayToastOffset(updateOverlayHeight),
            }}
            mobileOffset={{
              right: "1rem",
              bottom: updateOverlayToastOffset(updateOverlayHeight),
            }}
          />
        </ForkDialogContextProvider>
      </TerminalFirstContextProvider>
    </FileViewerContext.Provider>
  );
}

/**
 * Initial sidebar open state — open on desktop, closed on mobile. SSR-
 * safe (returns false when window is undefined). The threshold (`md`)
 * matches Tailwind's default 768px, used in the Sidebar's responsive
 * classes.
 */
function initialSidebarOpen(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(min-width: 768px)").matches;
}
