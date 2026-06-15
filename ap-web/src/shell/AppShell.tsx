import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Outlet, useParams, useSearchParams } from "@/lib/routing";
import { useConversations } from "@/hooks/useConversations";
import { useSessionAgent } from "@/hooks/useAgents";
import { AgentInfoContent, agentHasInfo } from "@/components/AgentInfo";
import { useIdleNotifications } from "@/hooks/useIdleNotifications";
import { readFilesPanelPreferences, writeFilesPanelPreferences } from "@/lib/filesPanelPreferences";
import { derivePermissionLevel, isOwnerLevel } from "@/lib/permissionsApi";
import { isMacElectronShell } from "@/lib/nativeBridge";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
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
import {
  AGENT_TERMINAL_IDS,
  inventoryTerminals,
  isAgentTerminalKey,
  PANEL_NO_TERMINAL_KEY,
  terminalTabKey,
  useTerminals,
} from "@/hooks/useTerminals";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { livenessRowFromSession, useSessionLiveness } from "@/hooks/useSessionLiveness";
import { useResizableInlinePanel } from "@/hooks/useResizableInlinePanel";
import { ChatHeader } from "./ChatHeader";
import { ExecutionLogsPanel } from "./ExecutionLogsPanel";
import { FileViewer } from "./FileViewer";
import { FileViewerContext } from "./FileViewerContext";
import { FilesPanelDrawer } from "./FilesPanelDrawer";
import type { ChangedSort } from "./FlatFileList";
import { MobilePanelDrawer } from "./MobilePanelDrawer";
import { Sidebar } from "./Sidebar";
import { TitleBarServerPicker } from "./TitleBarServerPicker";
import { SubagentsPanel } from "./SubagentsPanel";
import { useRootSessionId, useSession } from "@/hooks/useSession";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";
import { TerminalsPanel } from "./TerminalsPanel";
import { TodoPanel } from "./TodoPanel";
import { PermissionsModal } from "@/components/PermissionsModal";
import { ForkSessionDialog } from "./ForkSessionDialog";
import { ForkDialogContextProvider, type ForkDialogContextValue } from "./ForkDialogContext";
import { WorkspacePanel } from "./WorkspacePanel";
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
 * The right slot holds either `FilesPanel` (file tree) or `FileViewer`
 * (code + comments) — never both at once. Selecting a file transitions
 * from the tree view to the code view in the same slot. The "← Back"
 * button in `FileViewer` returns to the file tree.
 *
 * Default open state is taken from the initial viewport: the left sidebar is
 * open on desktop and closed on mobile. The right files panel starts closed.
 *
 * **Mobile session-rail entry**: the desktop right column has no room on
 * a phone, so the rail's contents are reached via a top-right FAB that
 * opens a dropdown with "Files" and "Terminals" (the latter only when
 * one or more terminals exist). Each entry opens the matching push
 * panel — the FAB and the desktop rail cards route through the same
 * open*() handlers.
 *
 * **Right rail tabs (desktop)**: the aside is internally tabbed between
 * Files, Terminals and Agents so each can claim the full rail height
 * instead of competing for a vertically-split slot. Files is the default;
 * within it a "Changed only" toggle filters the full folder tree down to
 * just the changed files (flat list). Opening a file (chat link or rail
 * click) forces the rail to the Files tab so the viewer is visible.
 * Terminal-first sessions render the terminal inline in main and therefore
 * hide the rail's Terminals tab. The Agents tab only appears once there's
 * more than one agent (the root has at least one child).
 */
export function AppShell() {
  // Read early: the conversationId scopes the per-session workspace state
  // (rail open/width/tab/open files) used throughout this component.
  const { conversationId } = useParams<{ conversationId: string }>();
  const [fileViewerCommentsOpen, setFileViewerCommentsOpen] = useState(false);
  const [rightRailTab, setRightRailTab] = useState<RightRailTab>(() =>
    conversationId ? (readSessionWorkspaceState(conversationId).rightRailTab ?? "files") : "files",
  );
  // The comments panel only contributes to the min width when the rail is
  // actually showing the file viewer — on the Terminals tab the FileViewer
  // is unmounted, so the 720 floor would just waste horizontal space.
  // 240px (CommentsPanel default/min width) + 480px comfortable code viewer
  // width. The panel can be dragged wider, but this floor keeps it usable at
  // its default; widening past it is the user's choice via the inline handle.
  const inlinePanelMinWidth = rightRailTab === "files" && fileViewerCommentsOpen ? 720 : undefined;
  const { panelWidth: inlinePanelWidth, handleProps: inlinePanelHandleProps } =
    useResizableInlinePanel(conversationId ?? null, inlinePanelMinWidth);
  const [searchParams, setSearchParams] = useSearchParams();
  const [sidebarOpen, setSidebarOpen] = useState(initialSidebarOpen);
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
  // false = full folder tree ("All"), true = changed-files-only flat list.
  // Surfaced as the Changed | All toggle inside the Files panel. Seeded from
  // the persisted, app-global preference (defaults to "All") so the choice
  // carries over as the user switches in and out of sessions and survives a
  // refresh. A deep-link ?view= URL param overrides it transiently below.
  //
  // The remembered scope is also held in a ref, mirroring FileViewer's
  // persistedPrefsRef. It's the in-memory source of truth the conversation-
  // switch effect falls back to — so a toggle is preserved across switches
  // even if the localStorage write was swallowed (Safari private mode /
  // blocked storage), rather than re-reading storage and reverting to the
  // default each switch. The lazy useState initializer reads localStorage
  // exactly once; the ref seeds from that initial value (useRef ignores its
  // argument after mount), so we don't re-read storage on every render.
  const [filesPanelFlatView, setFilesPanelFlatView] = useState(
    () => readFilesPanelPreferences().changedOnly,
  );
  const filesPanelScopePrefRef = useRef(filesPanelFlatView);
  // Tracks which conversation the current rail tab / open-files state belongs
  // to, so the persist effect targets the right session even before a switch's
  // restore has re-rendered. Set by the conversation-restore effect.
  const stateConvRef = useRef<string | null>(conversationId ?? null);
  // Skips the first persist run so mount can't write default state over the
  // values the restore effect is about to apply.
  const workspacePersistHydrated = useRef(false);
  const [filesPanelShowHidden, setFilesPanelShowHidden] = useState(false);
  // Lifted so the Changes list order and the FileViewer prev/next order
  // share one source of truth (otherwise the "X/N" index won't match the
  // list position). Default "recent" mirrors the prior FilesPanel default.
  const [filesPanelSort, setFilesPanelSort] = useState<ChangedSort>("recent");
  const [panelInitialKey, setPanelInitialKeyState] = useState<string | null>(null);
  const [executionLogsKey, setExecutionLogsKey] = useState<string | null>(null);
  const [filesPanelOpen, setFilesPanelOpen] = useState(false);
  // Mobile-only full-screen drawers for the rail tabs that have no desktop
  // push panel of their own. On desktop these are tabs in the workspace rail;
  // on a phone they open as full-screen overlays from the session-menu FAB.
  const [subagentsPanelOpen, setSubagentsPanelOpen] = useState(false);
  const [todosPanelOpen, setTodosPanelOpen] = useState(false);
  // The right "Workspace" rail (WorkspacePanel) is open by default and
  // remembers its open/closed state per session — a brand-new session starts
  // open; reopening a session restores how the user last left it. Toggled
  // via the header's PanelRightIcon, mirroring the sidebar collapse. With no
  // conversation the rail can't render, so the state stays false — leaving it
  // true would let rail-gated side effects (the ?view= URL sync) fire on
  // non-session routes like the home page.
  const [rightPanelOpen, setRightPanelOpen] = useState(() =>
    conversationId ? (readSessionWorkspaceState(conversationId).open ?? true) : false,
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

  const debugMode = useDebugMode();
  const { data: conversationsData } = useConversations();
  // Surface sessions needing attention as OS notifications + a dock badge.
  // Mounted here (inside the Router) so it can navigate on click and knows
  // the active conversation id, which suppresses the notification/badge for
  // the session the user is actively viewing.
  useIdleNotifications(conversationId);
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
  const liveness = useSessionLiveness(
    conversationId ?? undefined,
    activeConv ?? livenessRowFromSession(activeSession),
    { turnActive: chatStatus === "streaming" },
  );
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
  const isNativeWrapper = isClaudeNative || sessionLabels["omnigent.wrapper"] === "codex-native-ui";
  const todos = useChatStore((s) => s.todos);
  const todosCompleted = todos.filter((t) => t.status === "completed").length;
  // Used for the header "Back to parent" link, which is hidden on
  // top-level sessions. The Subagents tab itself is always visible —
  // it lists the root's children plus a "main" entry, so the user
  // can move between siblings and back to the parent from one place.
  const isChildSession = activeSession?.parentSessionId != null;
  // Positive "this is a top-level session" signal for the top-level-only
  // actions (Share/Clone). Gating those on ``!isChildSession`` flickered:
  // while the snapshot loads ``activeSession`` is null, so ``isChildSession``
  // is false and the buttons rendered, then vanished once the snapshot
  // revealed ``parentSessionId``. A session present in the sidebar list is
  // known top-level immediately (children are omitted from that query);
  // otherwise we wait for the snapshot rather than optimistically showing.
  const isKnownTopLevel =
    activeConv != null || (activeSession != null && activeSession.parentSessionId == null);
  // Header action gating, hoisted so the desktop buttons and the mobile
  // three-dot menu render the exact same set (they can't drift apart).
  // Stop session is not a header action — it lives in the sidebar row's
  // kebab menu (see Sidebar's ConversationRow).
  // Read-write or higher can manage sharing; top-level only. Sharing a
  // sub-agent is a no-op anyway — children inherit the parent's grants via
  // the server's parent-delegation path — so we hide the affordance.
  const canShare =
    !!conversationId && isKnownTopLevel && (permissionLevel === null || permissionLevel >= 3);
  // Any viewer can fork a shared session; top-level only (the server
  // rejects forking a sub-agent). Surfaced as ForkDialogContext.canFork —
  // the per-message "Fork from here" action is the only fork entry point.
  const canClone =
    !!conversationId && isKnownTopLevel && (permissionLevel === null || permissionLevel >= 1);
  // Agent tools/policies exist to show.
  const hasAgentInfo = !!conversationId && agentHasInfo(boundAgent, conversationId);
  // Whether the mobile three-dot menu has any entry to offer.
  const hasHeaderMenu = canShare || hasAgentInfo;
  // Claude-native sub-agents have no terminal of their own — the parent
  // owns the tmux pane.
  const isClaudeNativeSubagent =
    activeSession?.labels?.["omnigent.wrapper"] === "claude-code-native-ui-subagent" ||
    activeConv?.labels?.["omnigent.wrapper"] === "claude-code-native-ui-subagent";
  // Hide the rail Shells tab only for claude-native sub-agents — they
  // have no terminals of their own (the parent owns the tmux pane).
  // Native top-level sessions get the same Shells rail as SDK ones;
  // their vendor pane is excluded from the inventory like the SDK
  // REPL (see ``inventoryTerminals``).
  const hideTerminalsTab = isClaudeNativeSubagent;
  // Inventory view of the terminal list for the rail tab, its badge,
  // and the mobile menu. The pill surfaces (``terminalsAvailable``,
  // ``setView``) keep the full ``terminals`` list so the agent's own
  // terminal stays openable through the Terminal pill.
  const railTerminals = useMemo(
    () => inventoryTerminals(terminals, terminalFirst),
    [terminals, terminalFirst],
  );
  // The agent's spec declares shell access (a ``terminals:`` block) —
  // the rail's Shells tab then shows BY DEFAULT, before any shell
  // exists: its empty state carries the "+ New shell" affordance, so
  // an empty tab is an entry point, not a dead end. Agents without
  // shell access only get the tab once a shell actually exists
  // (e.g. attached by other means).
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
  const rootSessionId = useMemo(() => {
    if (!conversationId) return null;
    // Snapshot resolved for a top-level session → it is its own root.
    if (activeSession && activeSession.parentSessionId == null) return conversationId;
    // Snapshot resolved for a descendant + walk complete → authoritative.
    if (activeSession && walkedRoot) return walkedRoot;
    // Snapshot or walk still loading → hold the last root while the target
    // is that root itself or a known member of its cached tree.
    const sticky = stickyRootRef.current;
    if (
      sticky !== null &&
      (sticky === conversationId ||
        cachedTreeContains(queryClient, sticky, conversationId, MAX_TREE_DEPTH))
    ) {
      return sticky;
    }
    // No sticky context (e.g. a deep link straight into a sub-agent):
    // fall back one hop until the walk resolves the true root.
    return activeSession?.parentSessionId ?? conversationId;
  }, [conversationId, activeSession, walkedRoot, queryClient]);
  // One-shot fetch (no polling) for the Subagents tab's count badge.
  // SubagentsPanel mounts its own polling usage of the hook against
  // the same rootSessionId, so the cache is shared.
  const { children: childSessions } = useChildSessions(rootSessionId);
  // Remember the resolved root so a later click into one of its tree
  // members can hold it steady (see ``rootSessionId`` above).
  useEffect(() => {
    stickyRootRef.current = rootSessionId;
  }, [rootSessionId]);
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
        // Agents tab is unconditional: the panel always lists at least
        // the main agent (its "main" row), so there's never a dead end.
        subagents: true,
        // Shells tab: shown by default when the agent's spec declares
        // shell access (the empty state offers "+ New shell"), or once a
        // shell exists for agents that don't. Inventory view: the
        // embedded REPL terminal of terminal-first SDK sessions doesn't
        // count — a session whose only terminal is the REPL and whose
        // agent has no shell access shows no tab. ``hideTerminalsTab``
        // is label-derived and starts false; ``railTerminals`` starts
        // empty and ``agentSupportsShells`` starts false while the agent
        // loads, so native sessions don't flash the tab.
        terminals: !hideTerminalsTab && (railTerminals.length > 0 || agentSupportsShells),
        todos: isClaudeNative && todos.length > 0,
      }) as const,
    [
      showFilesPanel,
      hideTerminalsTab,
      railTerminals.length,
      agentSupportsShells,
      isClaudeNative,
      todos.length,
    ],
  );
  // Whether the rail has anything at all to show. When false the workspace
  // card doesn't mount and the header hides its collapse toggle — a
  // no-filesystem agent with no terminals/sub-agents/todos would otherwise
  // render an empty white card with no way to dismiss it.
  const hasRailContent = Object.values(railTabsAvailable).some(Boolean);
  // Keep the selected tab valid. When the current tab disappears — files
  // panel turns off, or the Shells tab hides (native wrapper / no shell
  // and no shell access) — fall back to the first still-visible tab in
  // display order (Files · Agents · Shells · Tasks). Picking the first
  // available (rather than ping-ponging between two effects) keeps this
  // convergent even when several tabs vanish at once.
  useEffect(() => {
    if (railTabsAvailable[rightRailTab]) return;
    const next = (["files", "subagents", "terminals", "todos"] as const).find(
      (t) => railTabsAvailable[t],
    );
    if (next) setRightRailTab(next);
  }, [railTabsAvailable, rightRailTab]);

  // Build a stable Set of agent-changed file paths so the FileViewer context
  // can tell BlockRenderer which inline code spans are real workspace files.
  // We use the changed-files list (not the flat top-level directory listing)
  // because it contains full relative paths like `ap-web/src/shell/Foo.tsx`.
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
  // re-entering a native session doesn't drop the user back into chat
  // view. sessionStorage scope: same tab, cleared on tab close —
  // a deliberately narrow scope so a stale "terminal view" preference
  // can't survive across browser sessions, where the user's mental model
  // may have moved on.
  const setPanelInitialKey = useCallback(
    (key: string | null) => {
      setPanelInitialKeyState(key);
      if (!conversationId) return;
      const storageKey = `omnigent.ap-web.panel-key:${conversationId}`;
      if (key === null) {
        sessionStorage.removeItem(storageKey);
      } else {
        sessionStorage.setItem(storageKey, key);
      }
    },
    [conversationId],
  );

  // Restore the per-session workspace state when switching conversations:
  // rail open-state, selected tab, and the open file tabs. The Chat/TUI toggle
  // is restored from sessionStorage; the Files scope and the active file also
  // honor URL search params so shared links open to the right view.
  useEffect(() => {
    setExecutionLogsKey(null);
    setFilesPanelOpen(false);
    setSubagentsPanelOpen(false);
    setTodosPanelOpen(false);
    setFilesPanelShowHidden(false);
    if (!conversationId) {
      // No session → no rail; false (not the open default) so rail-gated
      // effects like the ?view= URL sync stay quiet on non-session routes.
      setRightPanelOpen(false);
      setRightRailTab("files");
      setSelectedFilePath(null);
      setOpenFiles([]);
      setPanelInitialKeyState(null);
      stateConvRef.current = null;
      return;
    }
    const persisted = readSessionWorkspaceState(conversationId);

    const stored = sessionStorage.getItem(`omnigent.ap-web.panel-key:${conversationId}`);
    setPanelInitialKeyState(stored);

    // Restore the Files view scope. A deep-link ?view= param wins and forces
    // the rail onto the Files tab: ?view=changed → "Changed" (flat list),
    // ?view=explore is the legacy tree param. With no param, fall back to the
    // user's remembered choice (defaults to "All") so the scope stays sticky
    // across session switches.
    const viewParam = searchParams.get("view");
    // ``nextTab`` stays null when there's no explicit signal to restore a tab
    // (no ?view=, no persisted tab, no file to surface). In that case we leave
    // ``rightRailTab`` untouched so the tab-fallback effect can still land on
    // the first *available* tab — forcing "files" here would shadow it.
    let nextTab: RightRailTab | null = null;
    if (viewParam === "changed") {
      setFilesPanelFlatView(true);
      nextTab = "files";
    } else if (viewParam === "explore") {
      setFilesPanelFlatView(false);
      nextTab = "files";
    } else {
      // Fall back to the remembered choice from the in-memory ref (not a
      // fresh localStorage read) so a swallowed write can't reset the scope.
      setFilesPanelFlatView(filesPanelScopePrefRef.current);
      nextTab = persisted.rightRailTab ?? null;
    }

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
    // A selected file must be visible in the rail. The Agents/Todos/Terminals
    // tabs don't render the inline viewer, so pull the rail to Files.
    if (nextSelected && nextTab !== "files") {
      nextTab = "files";
    }
    if (nextTab !== null) setRightRailTab(nextTab);

    // Restore the rail open-state for this session. A deep link / reload that
    // carries a workspace signal — a file to open (?file=), a files-scope view
    // (?view=changed|explore), or a comment to surface (?comment=) — reveals
    // the rail even when this session was last left closed; otherwise the
    // linked file/comment would render into a collapsed, invisible panel. This
    // is transient: it doesn't rewrite the session's saved open-state.
    const commentParam = searchParams.get("comment");
    const hasWorkspaceUrlSignal =
      urlFile !== null ||
      viewParam === "changed" ||
      viewParam === "explore" ||
      (commentParam !== null && commentParam !== "");
    setRightPanelOpen((persisted.open ?? true) || hasWorkspaceUrlSignal);

    stateConvRef.current = conversationId;
  }, [conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

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
    writeSessionWorkspaceState(id, { rightRailTab, openFiles, selectedFilePath });
  }, [rightRailTab, openFiles, selectedFilePath]);

  // Sync the Files-panel scope into the URL. The tree is the default, so we
  // only write the param for "Changed only" — and only while the rail is open,
  // since the scope is meaningless (and shouldn't deep-link the rail back open)
  // once the workspace is collapsed. Collapsing thus drops ?view= here.
  useEffect(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (rightPanelOpen && filesPanelFlatView) {
          next.set("view", "changed");
        } else {
          next.delete("view");
        }
        return next;
      },
      { replace: true },
    );
  }, [filesPanelFlatView, rightPanelOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  // Manual scope changes (the Changed | All toggle) persist the choice so it
  // sticks across session switches and page refreshes. Update the in-memory
  // ref too (the conversation-switch fallback), so the choice survives even if
  // the localStorage write is swallowed.
  const handleFilesFlatViewChange = useCallback((v: boolean) => {
    filesPanelScopePrefRef.current = v;
    setFilesPanelFlatView(v);
    writeFilesPanelPreferences({ changedOnly: v });
  }, []);

  const openFileViewer = useCallback(
    (path: string) => {
      setSelectedFilePath(path);
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
      setTodosPanelOpen(false); // close mobile tasks drawer
      // Pull the rail to the Files tab when parked on a tab where the viewer
      // won't render (Terminals, Subagents, Todos). The Files tab surfaces the
      // FileViewer inline, so leave it undisturbed.
      setRightRailTab((prev) =>
        prev === "terminals" || prev === "subagents" || prev === "todos" ? "files" : prev,
      );
      // Reveal the rail so the viewer is actually visible — the rail defaults
      // open but a session the user collapsed restores collapsed, so opening a
      // file (e.g. via a chat file-path link) there would otherwise route the
      // file into an invisible panel. Persist open=true so the rail stays in
      // sync with the open file on the next visit (mirroring the header
      // toggle's persistence).
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
  ); // eslint-disable-line react-hooks/exhaustive-deps

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
    [setSearchParams],
  ); // eslint-disable-line react-hooks/exhaustive-deps

  // Switch the workspace rail's tab. The side effect (closing any open
  // file + its comments + URL) lives here, not in WorkspacePanel, so the
  // tab state and the file state can't drift apart — the rail stays a
  // dumb view. A single Files tab now owns the viewer, so there's no
  // per-tab file to stash and restore; switching tabs just closes any
  // open file.
  function handleRightRailTabChange(next: RightRailTab) {
    setRightRailTab(next);
    if (selectedFilePath !== null) {
      setSelectedFilePath(null);
      setFileViewerCommentsOpen(false);
      clearFileViewerUrl();
    }
  }

  function openTerminalsPanel(key: string) {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setExecutionLogsKey(null); // close execution-logs panel
    setFilesPanelOpen(false); // close files drawer
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setTodosPanelOpen(false); // close mobile tasks drawer
    setPanelInitialKey(key);
  }

  function openExecutionLogsPanel(key: string) {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setFilesPanelOpen(false); // close files drawer
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setTodosPanelOpen(false); // close mobile tasks drawer
    setExecutionLogsKey(key);
  }

  // Mobile FAB → "Files" opens the files drawer (mirrors the desktop rail's
  // Files tab). The "Changed only" scope is the drawer's own toggle, shared
  // with the desktop rail via ``filesPanelFlatView``, so we don't force it.
  function openFilesPanel() {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setExecutionLogsKey(null); // close execution-logs panel
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setTodosPanelOpen(false); // close mobile tasks drawer
    setFilesPanelOpen(true);
  }

  // Mobile FAB → "Agents" opens the subagents list (the desktop rail's
  // Agents tab) as a full-screen drawer.
  function openSubagentsPanel() {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setExecutionLogsKey(null); // close execution-logs panel
    setFilesPanelOpen(false); // close files drawer
    setTodosPanelOpen(false); // close mobile tasks drawer
    setSubagentsPanelOpen(true);
  }

  // Mobile FAB → "Tasks" opens the todo list (the desktop rail's Tasks tab)
  // as a full-screen drawer.
  function openTodosPanel() {
    setSelectedFilePath(null); // close file viewer
    clearFileViewerUrl();
    setPanelInitialKey(null); // close terminals panel
    setExecutionLogsKey(null); // close execution-logs panel
    setFilesPanelOpen(false); // close files drawer
    setSubagentsPanelOpen(false); // close mobile agents drawer
    setTodosPanelOpen(true);
  }

  function openFirstTerminal() {
    // Mobile FAB → "Terminals" routes to the first terminal so the
    // single tap is equivalent to clicking the first row in the
    // desktop rail's terminals card. Inventory view — the embedded
    // REPL terminal is the pill's Terminal view, not a rail entry.
    if (railTerminals.length === 0) return;
    openTerminalsPanel(terminalTabKey(railTerminals[0]));
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
      if (terminals.length === 0) {
        if (terminalFirst) setPanelInitialKey(PANEL_NO_TERMINAL_KEY);
        return;
      }
      // The pill's Terminal view is the AGENT's terminal: target it
      // explicitly (the SDK REPL or the native vendor pane) so the
      // pill never lands on a user shell.
      const agentTerminal = terminals.find((t) => AGENT_TERMINAL_IDS.has(t.id));
      setPanelInitialKey(terminalTabKey(agentTerminal ?? terminals[0]));
    },
    [terminalFirst, terminals, setPanelInitialKey],
  );

  // `terminals` is already runner-accurate (useTerminals empties it when the
  // runner is offline), so a non-empty list means an openable PTY.
  const terminalsAvailable = terminals.length > 0;
  // Single pill-facing "loading" signal: not yet openable, but coming up —
  // either the runner is launching/relaunching (liveness `starting`, known the
  // instant a message is sent) or it's up and auto-creating the PTY
  // (`terminalPending`). Idle stopped sessions are neither → greyed, not spinning.
  // Suppressed once the session has failed: a runner that crashed before
  // connecting (`runner_failed_to_start`), or a host that refused the launch
  // (`harness_not_configured`), sits in the `starting` grace window but can
  // never come up — so drop the spinner the instant the failed status lands
  // and let the error banner stand alone. `sessionStatus` (declared above) is
  // set by both the live `session.status:failed` push and the snapshot reload.
  const terminalStartingUp =
    !terminalsAvailable &&
    sessionStatus !== "failed" &&
    (liveness.kind === "starting" || terminalPending);
  // A rail-opened shell (any open terminal key other than the agent's
  // own terminal) takes over the main view chrome-free:
  // ConnectionIndicator hides the Chat/Terminal pill while this is
  // true, and MainTerminalView renders the shell with its own close
  // affordance. The PANEL_NO_TERMINAL_KEY sentinel ("") is falsy, so
  // "open with no target" stays a pill view.
  const isShellView = terminalFirst && !!panelInitialKey && !isAgentTerminalKey(panelInitialKey);
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
            data-electron-mac={isMacElectronShell() ? "true" : undefined}
          >
            {/* Frameless-window titlebar stand-in (macOS Electron only): the
          sidebar's electron top margin (see index.css) frees this strip of
          canvas for the traffic lights, and the strip is the window's one
          drag surface — content below and right stays fully clickable. */}
            {isMacElectronShell() && <div className="electron-drag-strip" aria-hidden="true" />}
            {/* Centered title + server picker in the freed title-bar strip. The
          open thread's title (snapshot first — it's the only source for
          child sessions — then the sidebar row) replaces the brand label. */}
            {isMacElectronShell() && (
              <TitleBarServerPicker threadTitle={activeSession?.title ?? activeConv?.title} />
            )}
            <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

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
              >
                <ChatHeader
                  sidebarOpen={sidebarOpen}
                  onOpenSidebar={() => setSidebarOpen(true)}
                  isChildSession={isChildSession}
                  parentSessionId={activeSession?.parentSessionId}
                  conversationId={conversationId}
                  boundAgent={boundAgent}
                  canShare={canShare}
                  onShare={() => setShareOpen(true)}
                  hasAgentInfo={hasAgentInfo}
                  onAgentInfo={() => setAgentInfoOpen(true)}
                  hasHeaderMenu={hasHeaderMenu}
                  showFilesPanel={showFilesPanel}
                  hasRailContent={hasRailContent}
                  rightPanelOpen={rightPanelOpen}
                  onToggleRightPanel={() => {
                    const next = !rightPanelOpen;
                    if (conversationId) writeSessionWorkspaceState(conversationId, { open: next });
                    if (next) {
                      // Reopening lands back on the file remembered in per-session
                      // state, so re-add ?file= to keep the URL shareable — mirroring
                      // how the scope-sync effect re-adds ?view= on reopen. diff and
                      // comment are URL-only ephemerals (not remembered), so they
                      // intentionally don't rehydrate. Imperative (not an effect) to
                      // avoid the FileViewer diff-sync race documented in that effect.
                      if (selectedFilePath) {
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
                      // link params that point into it (file/diff/comment) — otherwise
                      // the URL advertises a file that isn't shown and a reload would
                      // re-open the rail. (?view= is dropped by the scope-sync effect,
                      // which is gated on rightPanelOpen.)
                      clearFileViewerUrl();
                    }
                    setRightPanelOpen(next);
                  }}
                  mobileMenu={{
                    fileViewerOpen,
                    panelOpen,
                    terminalFirst,
                    executionLogsOpen,
                    filesPanelOpen,
                    subagentsPanelOpen,
                    todosPanelOpen,
                    hideTerminalsTab,
                    terminalsLength: railTerminals.length,
                    isClaudeNative,
                    todosCompleted,
                    todosTotal: todos.length,
                    debugMode,
                    changedCount,
                    subagentsWorking,
                    agentCount,
                    onOpenFiles: openFilesPanel,
                    onOpenFirstTerminal: openFirstTerminal,
                    onOpenSubagents: openSubagentsPanel,
                    onOpenTodos: openTodosPanel,
                    onOpenMainExecutionLog: openMainExecutionLog,
                  }}
                />
                <main className="relative flex min-h-0 min-w-0 flex-1 flex-col">
                  <Outlet />
                </main>

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
                {conversationId &&
                  hasRailContent &&
                  rightPanelOpen &&
                  (terminalFirst || !panelOpen) &&
                  !executionLogsOpen &&
                  !filesPanelOpen && (
                    <WorkspacePanel
                      conversationId={conversationId}
                      width={inlinePanelWidth}
                      inert={inlinePanelWidth === 0}
                      handleProps={inlinePanelHandleProps}
                      rightRailTab={rightRailTab}
                      onRightRailTabChange={handleRightRailTabChange}
                      showFilesPanel={showFilesPanel}
                      changedCount={changedCount}
                      showShellsTab={railTabsAvailable.terminals}
                      terminalsLength={railTerminals.length}
                      subagentsWorking={subagentsWorking}
                      agentCount={agentCount}
                      isClaudeNative={isClaudeNative}
                      todosCompleted={todosCompleted}
                      todosTotal={todos.length}
                      rootSessionId={rootSessionId}
                      selectedFilePath={selectedFilePath}
                      openFiles={openFiles}
                      openFileViewer={openFileViewer}
                      onCloseFile={closeFile}
                      onShowScopeView={showScopeView}
                      onCommentsOpenChange={setFileViewerCommentsOpen}
                      openTerminalsPanel={openTerminalsPanel}
                      permissionLevel={permissionLevel}
                      filesPanelSort={filesPanelSort}
                      onSortChange={setFilesPanelSort}
                      filesPanelFlatView={filesPanelFlatView}
                      onFlatViewChange={handleFilesFlatViewChange}
                      filesPanelShowHidden={filesPanelShowHidden}
                      onShowHiddenChange={setFilesPanelShowHidden}
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
                  flatView={filesPanelFlatView}
                  onFlatViewChange={handleFilesFlatViewChange}
                  showHidden={filesPanelShowHidden}
                  onShowHiddenChange={setFilesPanelShowHidden}
                  sort={filesPanelSort}
                  onSortChange={setFilesPanelSort}
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
                  open={todosPanelOpen}
                  title="Tasks"
                  onClose={() => setTodosPanelOpen(false)}
                  testId="todos-panel-drawer"
                >
                  <TodoPanel frameless />
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
              sourceWorkspace={activeSession?.workspace}
              sourceHostId={activeSession?.hostId}
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
