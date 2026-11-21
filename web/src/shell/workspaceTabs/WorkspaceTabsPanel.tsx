// The workspace sidebar's tab strip and the selected place's content.

import { WorkspacePlaceView } from "./placeViews/WorkspacePlaceView";
import { TerminalTabStatus } from "./TerminalTabStatus";
import { type TerminalTabConnection, terminalTabSpins } from "./terminalTabConnection";
import { useTerminalTabConnections } from "./useTerminalTabConnection";
import { isPinnedTab } from "./pinnedTabs";
import { reorderStripEntries, type StripReorderEntry } from "./stripReorder";
import { pinnedTabsMaxWidth, TAB_GAP } from "./tabStripWidth";
import { useTabStripWidth } from "./useTabStripWidth";
import { type TabSearchEntry, WorkspaceTabSearch } from "./WorkspaceTabSearch";
import {
  NEW_TAB_DIVIDER_ROOM,
  NewTabButton,
  PINNED_TAB_WIDTH,
  StripTab,
  StripTabMenu,
  type TabTitle,
} from "./WorkspaceStripTab";
import {
  BotIcon,
  FileIcon,
  FileDiffIcon,
  FolderTreeIcon,
  GitPullRequestIcon,
  GlobeIcon,
  LayoutGridIcon,
  ListChecksIcon,
  type LucideIcon,
  MaximizeIcon,
  XIcon,
  MessageSquareIcon,
  MinimizeIcon,
  SearchIcon,
  SquareTerminalIcon,
} from "lucide-react";
import {
  type CSSProperties,
  memo,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { toast } from "sonner";

import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";
import { SandboxResumeNotice } from "@/components/SandboxResumeNotice";
import { BrowserPane } from "@/components/BrowserPane/BrowserPane";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useInertRef } from "@/hooks/useInertRef";
import { useSandboxResume } from "@/hooks/useSandboxResume";
import { useNewBrowserHotkey } from "@/hooks/useNewBrowserHotkey";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import { terminalDisplayLabel, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { AGENT_BROWSER_TAB_ID, browserViewId } from "@/lib/browserTabs";
import { closeBrowserView, forgetBrowserTab } from "@/hooks/useBrowserTabs";
import { isEditorLevel, isOwnerLevel } from "@/lib/permissionsApi";
import { shortcutAriaKeys, shortcutKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { supportsSideChat } from "@/lib/sideChat";
import { subagentSessionIdFromTab, subagentTabId } from "@/lib/subagentTabs";
import { ApiError, getSessionSlim } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { requestTextSearch, useFileFinderAvailable } from "@/store/fileFinder";
import { isCommandAvailable } from "@/store/paletteCommands";
import { usePullRequestListCount } from "@/store/pullRequestListCounts";

import { commandRegionProps } from "../commandContext";
import { ContentSearchPanel } from "../ContentSearchPanel";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { groupChangedFiles, type ChangedSort } from "../FlatFileList";
import { startSideChat } from "../sideChatLifecycle";
import { SUBAGENT_TAB_FALLBACK_LABEL, SubagentTabPane, useSubagentTabLabels } from "../SubagentTab";
import { type SubagentPanelOpener, SubagentPanelOpenerContext } from "../subagentPanelOpener";
import { SubagentsPanel } from "../SubagentsPanel";
import {
  type WorkspaceCommand,
  type WorkspaceCommandFacts,
  workspaceCloseCommands,
  workspaceFullScreenCommand,
  workspaceOpenCommands,
  workspaceShowCommands,
  browserTabInfo,
  type WorkspaceHistoryCommand,
  type WorkspaceHistoryDirection,
  TAB_NOT_CLOSABLE_REASON,
  tabActionCommandFor,
} from "../workspaceCommands/model";
import { useShellLauncher } from "../workspaceCommands/useShellLauncher";
import { useWorkspacePaletteCommands } from "../workspaceCommands/useWorkspacePaletteCommands";
import type { WorkspaceTabRunOrigin } from "../workspaceCommands/useWorkspaceTabChords";
import {
  WorkspaceSidebarFrame,
  type WorkspaceSidebarStripEntry,
  WorkspaceSidebarTabPanel,
} from "../workspaceSidebar/WorkspaceSidebarFrame";
import { UNSUPPORTED_TAB_TITLE, UnsupportedTabContent } from "../workspaceSidebar/unsupportedTab";
import { useWorkspaceSidebarTabActions } from "../workspaceSidebar/useWorkspaceSidebarTabActions";
import { useCanStartSideChat } from "@/hooks/usePermissions";
import { RailTerminalView, WorkspaceTabTooltip } from "../WorkspacePanel";
import { NewTabPage, type NewTabRecent } from "./NewTabPage";
import { recentPullRequestRows, recentPullRequestUrl } from "./recentPullRequestRow";
import { useRecentPullRequestFacts } from "./useRecentPullRequestFacts";
import { FileTypeIcon } from "@/shell/FileTypeIcon";
import { WorkspaceHistoryChevrons } from "./WorkspaceHistoryChevrons";
import { LOCAL_CHANGES_LABEL, type PlaceIcon, placeTitle, truncateMiddle } from "./placeTitle";
import type { TabId, WorkspacePlace } from "./workspacePlace";
import { isUnknownTab, type WorkspaceTab, type WorkspaceTabRecord } from "./workspaceTab";
import { isOnlyEmptyNewTab, LAST_NEW_TAB_REASON, type RecentPlace } from "./workspaceTabsStore";
import {
  type LeaveGuard,
  openWorkspaceFile,
  openWorkspaceLink,
  openWorkspaceNewTab,
  openWorkspacePage,
  type WorkspaceTabsSession,
  workspaceTabsSession,
} from "./workspaceTabsSessions";
import type { DefaultWorkspaceTab } from "@/lib/workspaceTabPreferences";
import { WORKSPACE_TEXT_SEARCH_COPY } from "@/lib/workspaceTextSearchCopy";
import { SessionDescendantRuns } from "@/components/workflows/WorkflowRunRow";
import { TasksPanel } from "../TasksPanel";
import { WORKSPACE_PANEL_TABS } from "../workspacePanelTabs";

export interface WorkspaceTabsPanelProps {
  conversationId: string;
  /** Keep the panel inside an independently scoped grid workspace. */
  contained?: boolean;
  /**
   * Docked in the rail beside chat (the default), or filling a drawer in the narrow
   * layout, which has no width to resize and no full screen to toggle.
   */
  placement?: "rail" | "drawer";
  /** Whether app-wide workspace hotkeys and ⌘K rows act here; a grid sets it on its selected cell. */
  keyboardTarget?: boolean;
  sessionHarness?: ReturnType<typeof useChatStore.getState>["sessionHarness"];
  agentId?: string | null;
  pending?: boolean;
  width: number;
  inert?: boolean;
  /** The rail is about to go inert, which its host defers; focus is kept out until it does. */
  inertPending?: boolean;
  open?: boolean;
  animateVisibility?: boolean;
  resizing?: boolean;
  handleProps: React.HTMLAttributes<HTMLDivElement> & { tabIndex: number };
  tabListRef?: RefObject<HTMLDivElement | null>;
  showFilesPanel: boolean;
  showContentSearch?: boolean;
  showGithubTab: boolean;
  /** Whether the session offers workflows, so it has a Tasks tab. */
  showTasksTab?: boolean;
  showBrowserTab: boolean;
  onReveal?: () => void;
  changedCount: number;
  /** The session's changed files, which a file's own Diffs tab counts its place among. */
  changedFiles: readonly WorkspaceChangedFile[];
  subagentsWorking: number;
  agentCount: number;
  rootSessionId: string | null;
  /**
   * The session's shells, as the classic strip lists them; null until the session's labels
   * are known, since until then its agent's own terminal can pass for a shell.
   */
  terminalKeys: readonly string[] | null;
  /** The shell the user just opened by hand, whose terminal may take focus. */
  autoFocusTerminalKey?: string | null;
  openTerminalTab: (key: string) => void;
  /** Ask before a shell ends; resolves false when the user keeps it. Ends nothing itself. */
  confirmCloseShell: (key: string) => Promise<boolean>;
  /** End a shell; false when it couldn't be ended. */
  endShell: (key: string) => Promise<boolean>;
  maximized: boolean;
  onToggleMaximized: () => void;
  /** Hides the sidebar, as the header's collapse button does. */
  onHide?: () => void;
  permissionLevel: number | null;
  filesPanelSort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  filesPanelShowHidden: boolean;
  onShowHiddenChange: (show: boolean) => void;
  liveness?: SessionLiveness;
  onShellCreateStart?: () => void;
  onShellCreateFailed?: () => void;
  subagentPanelOpener?: SubagentPanelOpener;
  /** Back and Forward, bound where they also work while the sidebar is closed. */
  historyCommands?: readonly WorkspaceHistoryCommand[];
  onHistoryGo?: (direction: WorkspaceHistoryDirection) => void;
}

const PLACE_ICONS: Record<PlaceIcon, LucideIcon> = {
  new: LayoutGridIcon,
  files: FolderTreeIcon,
  file: FileIcon,
  changes: FileDiffIcon,
  pulls: GitPullRequestIcon,
  pull: GitPullRequestIcon,
  terminal: SquareTerminalIcon,
  agents: BotIcon,
  tasks: ListChecksIcon,
  agent: BotIcon,
  chat: MessageSquareIcon,
  search: SearchIcon,
  web: GlobeIcon,
};

/** What the strip shows for a tab: its label, tooltip, icon and badge. */

/** The "+" button (24px) and its gap, then the divider after it, which share the tabs' row. */
const NEW_TAB_BUTTON_ROOM = 28 + NEW_TAB_DIVIDER_ROOM;

/** File names longer than this keep their start and extension around an ellipsis. */
const MIDDLE_TRUNCATE_LENGTH = 32;
const NO_HISTORY_COMMANDS: readonly WorkspaceHistoryCommand[] = [];

/** A session's tab store and editor guards, live. */
function useWorkspaceTabs(conversationId: string): {
  session: WorkspaceTabsSession;
  tabs: readonly WorkspaceTabRecord[];
  selectedTabId: TabId | null;
  canReopen: boolean;
  revision: number;
} {
  const session = useMemo(() => workspaceTabsSession(conversationId), [conversationId]);
  const snapshot = useSyncExternalStore(session.store.subscribe, session.store.snapshot);
  useSyncExternalStore(session.subscribe, session.version);
  return {
    session,
    tabs: snapshot.tabs,
    selectedTabId: snapshot.selectedTabId,
    canReopen: snapshot.canReopen,
    revision: snapshot.revision,
  };
}

function WorkspaceTabsPanelImpl({
  conversationId,
  contained = false,
  placement = "rail",
  keyboardTarget = !contained,
  sessionHarness,
  agentId,
  pending = false,
  width,
  inert,
  inertPending,
  open = true,
  animateVisibility = false,
  resizing = false,
  handleProps,
  tabListRef,
  showFilesPanel,
  showContentSearch = false,
  showGithubTab,
  showTasksTab = false,
  showBrowserTab,
  onReveal,
  changedCount,
  changedFiles,
  subagentsWorking,
  agentCount,
  rootSessionId,
  terminalKeys,
  autoFocusTerminalKey = null,
  openTerminalTab,
  confirmCloseShell,
  endShell,
  maximized,
  onToggleMaximized,
  onHide,
  permissionLevel,
  filesPanelSort,
  onSortChange,
  filesPanelShowHidden,
  onShowHiddenChange,
  liveness,
  onShellCreateStart,
  onShellCreateFailed,
  subagentPanelOpener,
  historyCommands = NO_HISTORY_COMMANDS,
  onHistoryGo,
}: WorkspaceTabsPanelProps) {
  const { session, tabs, selectedTabId } = useWorkspaceTabs(conversationId);
  const { store } = session;
  const knownTabs = tabs.filter((tab): tab is WorkspaceTab => !isUnknownTab(tab));
  const selectedTab = knownTabs.find((tab) => tab.id === selectedTabId);
  // The Pull requests list's own count; a tab showing one pull request has none.
  const pullRequestCount = usePullRequestListCount(conversationId);
  // Every strip entry but a pinned one shares one width. Pinned tabs keep their icon's
  // width and stay put while they all fit their share of the strip; past it, they scroll.
  const pinnedCount = tabs.filter(isPinnedTab).length;
  const pinnedNaturalWidth = pinnedCount === 0 ? 0 : pinnedCount * (PINNED_TAB_WIDTH + TAB_GAP) + 1;
  const pinsStayPutIn = (areaWidth: number) =>
    pinnedNaturalWidth <= pinnedTabsMaxWidth(areaWidth, NEW_TAB_BUTTON_ROOM);
  const stripWidth = useTabStripWidth(
    tabs.length - pinnedCount,
    (areaWidth) => NEW_TAB_BUTTON_ROOM + (pinsStayPutIn(areaWidth) ? pinnedNaturalWidth : 0),
  );
  const reorderEntries: StripReorderEntry<TabId>[] = tabs.map((tab) => ({
    key: tab.id,
    tabIds: [tab.id],
  }));
  const stripValue = selectedTabId ?? "";
  // The rail goes inert a frame late (WorkspaceTabsHost); focus stays out meanwhile.
  const railInertRef = useInertRef<HTMLElement>(inert ?? false, undefined, {
    pending: inertPending ?? !open,
  });

  const terminalsKnown = terminalKeys !== null;
  const terminalSignature = terminalKeys?.join("\n") ?? "";
  const {
    terminals,
    listCompleteAt,
    isFetching: terminalsFetching,
    error: terminalsError,
  } = useTerminals(pending ? null : conversationId);
  const terminalsLoaded = (listCompleteAt ?? 0) > 0 && terminalsError === null;
  const shellOf = (tab: WorkspaceTab, key: string) => ({
    key,
    inventory: store.resourceState(tab.id),
  });
  // One resolved connection per shell tab, which its body and its strip icon share.
  const tabConnections = useTerminalTabConnections(conversationId, store.shellWaits, {
    liveness: liveness?.kind ?? "unknown",
    // A failed list that's being asked for again connects until the answer comes.
    listFailed: terminalsError !== null && !terminalsFetching,
    shells: knownTabs.flatMap((tab) =>
      tab.binding?.kind === "terminal" ? [shellOf(tab, tab.binding.key)] : [],
    ),
  });
  const retryTerminals = tabConnections.tryAgain;
  const resumeSession = () => void tabConnections.resumeSession();
  // Shell tabs that have shown their terminal, by tab and shell, so a failed list refresh
  // keeps them mounted. Recorded after commit and pruned to the tabs still open.
  const shellTabKeys = knownTabs.flatMap((tab) =>
    tab.binding?.kind === "terminal" ? [attachedShellKey(tab.id, tab.binding.key)] : [],
  );
  const liveShellTabKeys = knownTabs.flatMap((tab) =>
    tab.binding?.kind === "terminal" && store.resourceState(tab.id) === "live"
      ? [attachedShellKey(tab.id, tab.binding.key)]
      : [],
  );
  const shellTabsSignature = shellTabKeys.join("\n");
  const liveShellTabsSignature = liveShellTabKeys.join("\n");
  const [attachedShells, setAttachedShells] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    const openShells = new Set(shellTabsSignature ? shellTabsSignature.split("\n") : []);
    const live = liveShellTabsSignature ? liveShellTabsSignature.split("\n") : [];
    setAttachedShells((previous) => {
      const next = new Set([...previous].filter((key) => openShells.has(key)));
      for (const key of live) next.add(key);
      const unchanged = next.size === previous.size && [...next].every((key) => previous.has(key));
      return unchanged ? previous : next;
    });
  }, [shellTabsSignature, liveShellTabsSignature]);
  useEffect(() => {
    if (pending) return;
    store.setTerminalInventory(
      terminalsLoaded && terminalsKnown
        ? { status: "loaded", keys: terminalSignature ? terminalSignature.split("\n") : [] }
        : { status: "unknown" },
    );
  }, [store, terminalsLoaded, terminalsKnown, terminalSignature, pending]);

  // Side-chat and sub-agent tabs go once their session is known not to exist; a
  // lookup that fails for any other reason keeps them.
  const sessionIdsSignature = knownTabs
    .flatMap((tab) =>
      (tab.binding?.kind === "chat" || tab.binding?.kind === "agent") && tab.binding.id
        ? [tab.binding.id]
        : [],
    )
    .sort()
    .join("\n");
  useEffect(() => {
    if (pending || !sessionIdsSignature) return;
    let active = true;
    for (const sessionId of sessionIdsSignature.split("\n")) {
      getSessionSlim(sessionId).then(
        () => active && store.setSessionState(sessionId, "live"),
        (error: unknown) => {
          if (!active) return;
          const gone = error instanceof ApiError && (error.status === 404 || error.status === 410);
          store.setSessionState(sessionId, gone ? "gone" : "unknown");
        },
      );
    }
    return () => {
      active = false;
    };
  }, [store, sessionIdsSignature, pending]);

  const terminalLabel = useCallback(
    (key: string) => {
      const terminal = terminals.find((candidate) => terminalTabKey(candidate) === key);
      return terminal ? terminalDisplayLabel(terminal) : key.replace(/^terminal:/, "");
    },
    [terminals],
  );
  const agentTabIds = knownTabs.flatMap((tab) =>
    tab.binding?.kind === "agent" ? [subagentTabId(tab.binding.id)] : [],
  );
  const agentLabels = useSubagentTabLabels(agentTabIds);
  // Recent pull requests on any new-tab page, named from what the session already knows of them.
  const recentPullRequestUrls = [
    ...new Set(
      knownTabs.flatMap((tab) =>
        tab.place.kind === "new"
          ? store.recentPlaces(tab.id).flatMap(({ place }) => recentPullRequestUrl(place) ?? [])
          : [],
      ),
    ),
  ];
  const pullRequestFacts = useRecentPullRequestFacts(conversationId, recentPullRequestUrls);
  const pullRequestRows = recentPullRequestRows(recentPullRequestUrls, pullRequestFacts);
  const chatTabs = knownTabs.filter((tab) => tab.binding?.kind === "chat");
  // In the order the review lists them, so a file tab's "2/3" matches what it shows.
  const orderedChangedPaths = useMemo(
    () =>
      groupChangedFiles([...changedFiles], filesPanelSort).flatMap((group) =>
        group.files.map((file) => file.path),
      ),
    [changedFiles, filesPanelSort],
  );
  const filePaths = knownTabs.flatMap((tab) =>
    tab.place.kind === "files" && tab.place.path !== undefined ? [tab.place.path] : [],
  );
  const titleOf = (tab: WorkspaceTab): TabTitle => {
    const { place, binding } = tab;
    const title = placeTitle(place, {
      otherFilePaths: filePaths.filter((path) => !(place.kind === "files" && place.path === path)),
      terminal: place.kind === "terminal" ? { label: terminalLabel(place.key) } : undefined,
      agentName: place.kind === "agent" ? agentLabels[subagentTabId(place.id)] : undefined,
      // Side chats are numbered in strip order, as the classic strip numbers them.
      chatTitle: place.kind === "chat" ? `Side chat ${chatTabs.indexOf(tab) + 1}` : undefined,
      // An attached shell shows its terminal, so its icon follows that, not the list.
      connecting:
        binding?.kind === "terminal" &&
        !attachedShells.has(attachedShellKey(tab.id, binding.key)) &&
        terminalTabSpins(tabConnections.connectionOf(shellOf(tab, binding.key))),
      // A session file tab's place among the changes; a range lists other files.
      changedFiles: place.kind === "changes" && !place.range ? orderedChangedPaths : undefined,
    });
    const reviewsEveryFile =
      place.kind === "changes" && (place.path === undefined || (!place.range && !place.fileTab));
    const countBadge =
      reviewsEveryFile && changedCount > 0
        ? String(changedCount)
        : place.kind === "agents" && agentCount > 0
          ? subagentsWorking > 0
            ? `${subagentsWorking}/${agentCount}`
            : String(agentCount)
          : place.kind === "pulls" && pullRequestCount
            ? String(pullRequestCount)
            : undefined;
    return {
      label:
        title.truncate === "middle"
          ? truncateMiddle(title.label, MIDDLE_TRUNCATE_LENGTH)
          : title.label,
      tooltip:
        binding?.kind === "browser-view" ? browserTabInfo(title.tooltip, true) : title.tooltip,
      icon: PLACE_ICONS[title.icon],
      // A file tab shows its type's colored icon, as the Files tree does.
      ...(title.icon === "file" && place.kind === "files" && place.path !== undefined
        ? { filePath: place.path }
        : {}),
      badge: title.badge ?? countBadge,
      connecting: title.connecting,
    };
  };

  /** A recent place as the new-tab page lists it: the open tab's own title, or the place's. */
  const recentOf = (recent: RecentPlace): NewTabRecent => {
    const openTab = recent.openTabId === undefined ? undefined : store.findTab(recent.openTabId);
    const title = openTab
      ? titleOf(openTab)
      : (() => {
          const placed = placeTitle(recent.place, {
            otherFilePaths: filePaths,
            agentName:
              recent.place.kind === "agent"
                ? agentLabels[subagentTabId(recent.place.id)]
                : undefined,
            chatTitle: recent.place.kind === "chat" ? "Side chat" : undefined,
            connecting: false,
          });
          return { ...placed, icon: PLACE_ICONS[placed.icon] };
        })();
    const path =
      recent.place.kind === "files" && recent.place.path !== undefined
        ? recent.place.path
        : undefined;
    const folder = path?.includes("/") ? path.slice(0, path.lastIndexOf("/")) : undefined;
    const Icon = title.icon;
    const pullRequestUrl = recentPullRequestUrl(recent.place);
    const pullRequest =
      pullRequestUrl === undefined ? undefined : pullRequestRows.get(pullRequestUrl);
    return {
      identity: recent.identity,
      label: title.label,
      ...(folder ? { description: folder } : {}),
      ...pullRequest,
      icon:
        path !== undefined ? (
          <FileTypeIcon path={path} />
        ) : (
          <Icon aria-hidden className="size-4 text-muted-foreground" />
        ),
    };
  };

  // ── Opening ──────────────────────────────────────────────────────────────
  const reportRefusal = (result: { ok: boolean } | { ok: false; reason: string }) => {
    if (!result.ok && "reason" in result) toast.error(result.reason);
  };

  const addBrowser = () => {
    void store
      .open({ place: { kind: "web" } }, { opener: "user", disposition: "new" })
      .then(reportRefusal);
    onReveal?.();
  };
  const inDrawer = placement === "drawer";
  // Hotkeys and ⌘K rows act only while this panel is the keyboard target.
  const commandsActive = !pending && keyboardTarget;
  useNewBrowserHotkey(() => addBrowser(), showBrowserTab && commandsActive);
  const shellLauncher = useShellLauncher(conversationId, {
    liveness,
    canEdit: isEditorLevel(permissionLevel),
    onOpened: openTerminalTab,
    onCreateStart: onShellCreateStart,
    onCreateError: onShellCreateFailed,
  });
  const activeHarness = useChatStore((state) => state.sessionHarness);
  const sideChatHarness = sessionHarness === undefined ? activeHarness : sessionHarness;
  const activeAgentId = useChatStore((state) => state.boundAgentId);
  const parentAgentId = agentId === undefined ? activeAgentId : agentId;
  const canStartSideChat = useCanStartSideChat(conversationId, permissionLevel);
  // An empty tab offers Resume while the sandbox is stopped, which Terminal and Diffs need.
  const sandboxStatus = useSandboxResume(conversationId).status;
  const sideChatAvailable = supportsSideChat(sideChatHarness, canStartSideChat);

  // A side chat the server created, or "Ask in side chat", announces itself here.
  const sideChatToOpen = useChatStore((state) => state.sideChatToOpen);
  const clearSideChatToOpen = useChatStore((state) => state.clearSideChatToOpen);
  const awaitingDrafts = useRef<string[]>([]);
  useEffect(() => {
    if (sideChatToOpen === null || sideChatToOpen.parentId !== conversationId) return;
    const { childId } = sideChatToOpen;
    const subagentId = subagentSessionIdFromTab(childId);
    if (subagentId !== null) {
      void store.open({ place: { kind: "agent", id: subagentId } }, { opener: "user" });
    } else if (childId.startsWith("pending:")) {
      void store.open({ place: { kind: "chat" } }, { opener: "user", draftKey: childId });
    } else {
      const awaiting = awaitingDrafts.current.shift();
      const draft =
        awaiting === undefined
          ? undefined
          : store
              .tabs()
              .find(
                (tab): tab is WorkspaceTab =>
                  !isUnknownTab(tab) &&
                  tab.binding?.kind === "chat" &&
                  tab.binding.draftKey === awaiting,
              );
      if (draft && store.completeDraftChat(draft.id, childId)) {
        useChatStore.getState().clearSideChatComposer(awaiting!);
        store.select(draft.id);
      } else {
        void store.open({ place: { kind: "chat", id: childId } }, { opener: "user" });
      }
    }
    onReveal?.();
    clearSideChatToOpen();
  }, [sideChatToOpen, conversationId, store, onReveal, clearSideChatToOpen]);

  // The New tab's "Search text" opens the same finder as ⌘K and its shortcut, where offered.
  const textSearchAvailable = useFileFinderAvailable(conversationId);
  const contentRef = useRef<HTMLDivElement>(null);

  // ── Closing ──────────────────────────────────────────────────────────────
  const canCloseShells = isEditorLevel(permissionLevel);
  /** Why `tab` can't close: the only empty New tab stays, and a shell needs edit access. */
  const closeRefusalFor = (tab: WorkspaceTabRecord | undefined): string | undefined => {
    if (!tab) return undefined;
    if (isOnlyEmptyNewTab(tabs, tab.id)) return LAST_NEW_TAB_REASON;
    if (!isUnknownTab(tab) && tab.binding?.kind === "terminal" && !canCloseShells) {
      return TAB_NOT_CLOSABLE_REASON;
    }
    return undefined;
  };
  /**
   * A browser view goes after its tab, without holding the strip; a failure only says so.
   * Forgetting it is awaited so a failed save reaches the store's release log.
   */
  const releaseFor = (tab: WorkspaceTab) => {
    const binding = tab.binding;
    if (binding?.kind !== "browser-view") return undefined;
    return async () => {
      const closed = await closeBrowserView(conversationId, binding.viewKey);
      if (closed) await forgetBrowserTab(conversationId, binding.viewKey);
      else toast.error("Couldn't close the browser page. It may keep running until you quit.");
      return closed;
    };
  };
  /**
   * How closing a bound tab treats its resource: a shell asks, then ends, before its
   * tab goes; a browser view is released after. A side chat or sub-agent tab only
   * hides; the chat keeps running and can be opened again.
   */
  const closeStepsFor = (tab: WorkspaceTab) => {
    const binding = tab.binding;
    if (binding?.kind === "terminal") {
      return {
        confirm: () => confirmCloseShell(binding.key),
        teardown: () => endShell(binding.key),
      };
    }
    return { release: releaseFor(tab) };
  };
  const newTab = () => {
    // "+" and its chord add the new tab at the far end, past every open tab.
    void openWorkspaceNewTab(conversationId);
    onReveal?.();
  };

  // The strip's tab list, which the chevron and ⌘K's "Search tabs…" open.
  const [tabSearchOpen, setTabSearchOpen] = useState(false);
  const stripEntryIds = tabs.map((tab) => tab.id);
  const actions = useWorkspaceSidebarTabActions({
    store,
    scopeId: commandsActive ? conversationId : null,
    stripWidth,
    sidebarOpen: open,
    entryIds: stripEntryIds,
    selectedEntryId: stripValue === "" ? null : stripValue,
    selectedLabel: selectedTab ? titleOf(selectedTab).label : undefined,
    closeRefusalFor: (tabId) => closeRefusalFor(tabs.find((tab) => tab.id === tabId)),
    canClose: (tab) => tab.binding?.kind !== "terminal" || canCloseShells,
    closeStepsFor,
    releaseFor,
    offers: { newTab, searchTabs: () => setTabSearchOpen(true) },
    pinning: {},
    onReveal,
  });
  const closeTab = actions.closeTab;

  // ── Commands, shared by "+", the menus and ⌘K ────────────────────────────
  const openablePanels = (["files", "changes", "github", "subagents", "tasks"] as const)
    .filter((page) => {
      if (page === "subagents") return true;
      if (page === "github") return showGithubTab;
      if (page === "tasks") return showTasksTab;
      return showFilesPanel;
    })
    // ⌘K, the "+" menu, the new-tab page and the tab all name Changes as local work.
    .map((tab) => ({
      tab,
      label: tab === "changes" ? LOCAL_CHANGES_LABEL : WORKSPACE_PANEL_TABS[tab].label,
    }));
  const facts: WorkspaceCommandFacts = {
    openablePanels,
    browser: showBrowserTab,
    sideChat: sideChatAvailable,
    shells: shellLauncher.shells,
    openPanels: tabs.map((tab) => {
      const known = isUnknownTab(tab) ? null : tab;
      const title = known ? titleOf(known) : { label: "Unsupported tab", tooltip: "" };
      return {
        key: tab.id,
        label: title.label,
        active: tab.id === selectedTabId,
        closing: false,
        closable: closeRefusalFor(tab) === undefined,
      };
    }),
    // A grid cell leaves the new-shell chord to the routed session; its browser chord is its own.
    boundChords: { shell: !contained, browser: commandsActive },
    fullScreen: inDrawer ? null : maximized,
  };
  const openCommands = workspaceOpenCommands(facts);
  const showCommands = workspaceShowCommands(facts);
  const closeCommands = workspaceCloseCommands(facts);
  const fullScreenCommand = workspaceFullScreenCommand(facts);
  const tabActionCommands = actions.commands;
  const reopenCommand = tabActionCommandFor(tabActionCommands, "reopen");
  const tabSearchEntries: TabSearchEntry[] = tabs.flatMap((tab) => {
    if (isUnknownTab(tab)) return [];
    const title = titleOf(tab);
    return [
      {
        tabId: tab.id,
        label: title.label,
        tooltip: title.tooltip,
        icon: title.icon,
        ...(title.filePath !== undefined ? { filePath: title.filePath } : {}),
        selected: tab.id === selectedTabId,
      },
    ];
  });
  // A launch from a new-tab page turns that tab into the shell once it opens.
  const runCommand = (
    command: WorkspaceCommand,
    fromNewTab?: TabId,
    origin?: WorkspaceTabRunOrigin,
  ) => {
    if (!isCommandAvailable(command.availability)) return;
    switch (command.kind) {
      case "panel":
        if (fromNewTab) {
          store.fillNewTab(fromNewTab, pagePlace(command.tab));
          return;
        }
        void openWorkspacePage(conversationId, command.tab).then(reportRefusal);
        onReveal?.();
        return;
      case "browser":
        if (fromNewTab) {
          store.fillNewTab(fromNewTab, { kind: "web" });
          return;
        }
        addBrowser();
        return;
      case "side-chat":
        if (fromNewTab) {
          store.fillNewTab(fromNewTab, { kind: "chat" });
          return;
        }
        void store.open({ place: { kind: "chat" } }, { opener: "user" }).then(reportRefusal);
        onReveal?.();
        return;
      case "shell":
        session.setPendingShellTab(fromNewTab ?? null);
        shellLauncher.launch(command.shell, { remember: !command.isDefault });
        return;
      case "show":
        store.select(command.panelKey);
        onReveal?.();
        return;
      case "close":
        closeTab(command.panelKey);
        return;
      case "full-screen":
        onToggleMaximized();
        return;
      case "tab-action":
        actions.run(command, origin);
        return;
    }
  };
  useWorkspacePaletteCommands({
    conversationId,
    // With the sidebar closed, New workspace tab still opens it; the rest say why they wait.
    commands: !commandsActive
      ? []
      : open
        ? [...openCommands, ...showCommands, ...closeCommands, ...tabActionCommands]
        : tabActionCommands,
    onRun: (command) => runCommand(command),
  });
  // ── Content ──────────────────────────────────────────────────────────────
  // Pages keep their tree, list and scroll while another tab shows; dirty editors stay mounted.
  const [visitedPages, setVisitedPages] = useState<ReadonlySet<TabId>>(() => new Set());
  useEffect(() => {
    if (!selectedTab || selectedTab.binding) return;
    const keepsState = ["files", "changes", "search", "pulls", "agents", "tasks"].includes(
      selectedTab.place.kind,
    );
    if (!keepsState || visitedPages.has(selectedTab.id)) return;
    setVisitedPages((current) => new Set([...current, selectedTab.id]));
  }, [selectedTab, visitedPages]);
  const dirtyTabIds = session.dirtyTabIds();
  const mountedTabs = knownTabs.filter(
    (tab) => tab.id === selectedTabId || visitedPages.has(tab.id) || dirtyTabIds.has(tab.id),
  );

  // One stable registrar per tab, so an editor registers its guard once per dirty spell.
  const leaveGuardRegistrars = useRef(new Map<TabId, (guard: LeaveGuard) => () => void>());
  const leaveGuardRegistrar = (tabId: TabId) => {
    let registrar = leaveGuardRegistrars.current.get(tabId);
    if (!registrar) {
      registrar = (guard) => session.registerLeaveGuard(tabId, guard);
      leaveGuardRegistrars.current.set(tabId, registrar);
    }
    return registrar;
  };
  useEffect(() => {
    leaveGuardRegistrars.current.clear();
  }, [session]);

  const renderTabContent = (tab: WorkspaceTab, selected: boolean): ReactNode => {
    const active = selected && open && !inert;
    const { place } = tab;
    switch (place.kind) {
      case "new":
        return (
          <NewTabPage
            place={place}
            onPlaceChange={(next) => void store.navigate(tab.id, next, { replace: true })}
            commands={openCommands}
            textSearch={textSearchAvailable}
            onTextSearch={() => requestTextSearch(conversationId)}
            onOpenPlace={(next) => {
              store.fillNewTab(tab.id, next);
            }}
            onRunCommand={(command) => runCommand(command, tab.id)}
            recents={store.recentPlaces(tab.id).map(recentOf)}
            onOpenRecent={(identity) => {
              const result = store.openRecent(tab.id, identity);
              if (!result.ok) toast.error(result.reason);
            }}
            onDismiss={tabs.length > 1 ? () => closeTab(tab.id) : undefined}
            sandbox={sandboxStatus}
            notice={
              <SandboxResumeNotice
                sessionId={conversationId}
                layout="banner"
                className="rounded-lg border"
              />
            }
            // A New tab the user opened takes focus; one left by the last close doesn't.
            autoFocus={active && tab.openedBy !== "restore"}
          />
        );
      case "files":
      case "changes":
      case "pulls":
      case "pull": {
        if ((place.kind === "files" || place.kind === "changes") && !showFilesPanel) {
          return (
            <Unavailable
              text={
                place.kind === "files"
                  ? "This session has no files to show."
                  : "This session has no changes to show."
              }
            />
          );
        }
        if ((place.kind === "pulls" || place.kind === "pull") && !showGithubTab) {
          return <Unavailable text="This session has no pull requests to show." />;
        }
        const view = (
          <WorkspacePlaceView
            conversationId={conversationId}
            place={place}
            filesTab={tab.role === "files"}
            active={active}
            keyboardTarget={commandsActive}
            onNavigate={(next, navigateOptions) =>
              void store.navigate(tab.id, next, navigateOptions).then(reportRefusal)
            }
            onOpen={(link, openOptions) => {
              void openWorkspaceLink(conversationId, link, openOptions).then(reportRefusal);
              onReveal?.();
            }}
            files={{
              sort: filesPanelSort,
              onSortChange,
              showHidden: filesPanelShowHidden,
              onShowHiddenChange,
              permissionLevel,
            }}
            position={session.fileRequest(tab.id)?.position}
            landing={store.landing(tab.id)}
            registerLeaveGuard={leaveGuardRegistrar(tab.id)}
          />
        );
        return place.kind === "pulls" || place.kind === "pull" ? (
          <RenderErrorBoundary name="pull requests" resetKey={conversationId}>
            {view}
          </RenderErrorBoundary>
        ) : (
          view
        );
      }
      case "tasks":
        return (
          <RenderErrorBoundary name="tasks" resetKey={conversationId}>
            <TasksPanel
              conversationId={conversationId}
              active={active}
              renderWorkflowRuns={(workflowId) => (
                <SessionDescendantRuns sessionId={conversationId} workflowId={workflowId} />
              )}
            />
          </RenderErrorBoundary>
        );
      case "agents":
        if (!rootSessionId) return <Unavailable text="This session has no agents yet." />;
        return (
          <SubagentPanelOpenerContext.Provider value={subagentPanelOpener ?? null}>
            <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
          </SubagentPanelOpenerContext.Provider>
        );
      case "search":
        if (!showContentSearch || !showFilesPanel) {
          return (
            <Unavailable
              text={`The ${WORKSPACE_TEXT_SEARCH_COPY.panel} isn't available in this session.`}
            />
          );
        }
        return (
          <ContentSearchPanel
            conversationId={conversationId}
            onFileSelect={(path, fileOptions) =>
              void openWorkspaceFile(conversationId, path, fileOptions, "user").then(reportRefusal)
            }
          />
        );
      case "terminal": {
        const shellState = store.resourceState(tab.id);
        // Until the shell list confirms it, a shell isn't ended; an attached one stays put.
        if (
          tab.binding?.kind !== "terminal" ||
          shellState === "gone" ||
          (shellState !== "live" && !attachedShells.has(attachedShellKey(tab.id, tab.binding.key)))
        ) {
          const connection: TerminalTabConnection =
            tab.binding?.kind === "terminal"
              ? tabConnections.connectionOf(shellOf(tab, tab.binding.key))
              : { kind: "ended" };
          return (
            <TerminalTabStatus
              connection={connection}
              sandboxNotice={tabConnections.sandboxNotice(connection)}
              onResumeSession={isEditorLevel(permissionLevel) ? resumeSession : undefined}
              resumingSession={tabConnections.resumingSession}
              onTryAgain={retryTerminals}
              // Only the owner can attach a runner, so only the owner gets the command.
              localReconnectCommand={
                isOwnerLevel(permissionLevel) ? tabConnections.localReconnectCommand : undefined
              }
            />
          );
        }
        return (
          <RailTerminalView
            conversationId={conversationId}
            terminalKey={tab.binding.key}
            readOnly={!isOwnerLevel(permissionLevel)}
            autoFocus={active && autoFocusTerminalKey === tab.binding.key}
          />
        );
      }
      case "web": {
        if (!showBrowserTab || tab.binding?.kind !== "browser-view") {
          return <Unavailable text="Web pages open in the desktop app." />;
        }
        const { viewKey } = tab.binding;
        return (
          <BrowserPane
            key={viewKey}
            conversationId={browserViewId(conversationId, viewKey)}
            agentBrowser={viewKey === AGENT_BROWSER_TAB_ID}
            active={active}
            className="min-h-0 flex-1"
          />
        );
      }
      case "chat": {
        if (tab.binding?.kind !== "chat") return null;
        const binding = tab.binding;
        const childId = binding.id ?? binding.draftKey;
        return (
          <SideChatPane
            key={childId}
            childId={childId}
            selectionParentId={active ? conversationId : undefined}
            onStart={(text) =>
              startSideChat({
                conversationId,
                harness: sideChatHarness,
                parentAgentId,
                draftKey: binding.draftKey,
                text,
                awaitingDrafts,
                onCreated: (createdId) => {
                  store.completeDraftChat(tab.id, createdId);
                },
              })
            }
          />
        );
      }
      case "agent":
        return (
          <SubagentTabPane
            key={place.id}
            sessionId={place.id}
            label={agentLabels[subagentTabId(place.id)] ?? SUBAGENT_TAB_FALLBACK_LABEL}
          />
        );
    }
  };

  const selectedUnknown = tabs.find((tab) => tab.id === selectedTabId && isUnknownTab(tab));
  const effectiveHandleProps = pending
    ? {
        ...handleProps,
        onMouseDown: undefined,
        onPointerDown: undefined,
        onPointerMove: undefined,
        onPointerUp: undefined,
        onPointerCancel: undefined,
        onLostPointerCapture: undefined,
        onKeyDown: undefined,
        "aria-disabled": true,
        tabIndex: -1,
      }
    : handleProps;

  return (
    <>
      {/* The resize target is a real flex gutter beside the rail, as in WorkspacePanel: its
          bounded slivers stay outside both scroll containers. */}
      {!maximized && !inDrawer && handleProps.hidden !== true && (
        <div
          {...effectiveHandleProps}
          data-workspace-panel-resize-gutter
          className={cn(
            "relative z-50 hidden w-1 shrink-0 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50 data-dragging:bg-primary/50 md:block",
            pending && "cursor-default hover:bg-transparent active:bg-transparent",
          )}
        />
      )}
      <aside
        aria-label="Workspace"
        {...commandRegionProps("workspace")}
        ref={railInertRef}
        aria-hidden={!open}
        data-workspace-design="tabs"
        data-collapsed={width === 0 || undefined}
        data-maximized={maximized || undefined}
        // Its own name, not data-state: a data-state change restyles everything inside the rail.
        data-rail-state={open ? "open" : "closed"}
        data-animate-visibility={animateVisibility || undefined}
        data-resizing={resizing || undefined}
        className={
          inDrawer
            ? "workspace-panel-motion @container/rail relative flex h-full min-h-0 flex-col overflow-hidden bg-card"
            : cn(
                "workspace-panel-motion @container/rail relative z-40 hidden md:flex md:min-h-0 md:flex-col md:overflow-hidden md:border-l md:border-border md:bg-card",
                maximized ? "md:absolute md:inset-0" : "md:shrink-0",
                contained &&
                  "flex min-h-0 flex-col overflow-hidden border-l border-border bg-card shrink-0",
                contained && maximized && "absolute inset-0",
              )
        }
        style={
          maximized || inDrawer
            ? undefined
            : ({
                width: open ? width : 0,
                "--omnigent-reserved-width": `${width}px`,
              } as CSSProperties)
        }
      >
        <div
          data-workspace-panel-inner
          className="relative flex h-full min-h-0 shrink-0 flex-col"
          style={{ width: maximized || inDrawer ? "100%" : `calc(${width}px - 1px)` }}
        >
          <WorkspaceSidebarFrame
            stripWidth={stripWidth}
            tabListRef={tabListRef}
            leading={
              // ‹ › and the tab list sit together, without the strip's gap.
              <div className="flex shrink-0 items-center">
                {!pending && onHistoryGo && (
                  <WorkspaceHistoryChevrons commands={historyCommands} onGo={onHistoryGo} />
                )}
                {!pending && tabs.length > 0 && (
                  <WorkspaceTabSearch
                    open={tabSearchOpen}
                    onOpenChange={setTabSearchOpen}
                    entries={tabSearchEntries}
                    reopen={
                      reopenCommand.availability.available
                        ? {
                            label: reopenCommand.menuLabel,
                            ...(reopenCommand.shortcut
                              ? { shortcut: shortcutKeys(reopenCommand.shortcut) }
                              : {}),
                          }
                        : null
                    }
                    onChoose={(choice) => {
                      const command =
                        choice.kind === "reopen"
                          ? reopenCommand
                          : showCommands.find((candidate) => candidate.panelKey === choice.tabId);
                      if (command) runCommand(command);
                    }}
                  />
                )}
              </div>
            }
            tabs={
              pending
                ? null
                : {
                    value: stripValue,
                    onValueChange: (tabId) => {
                      store.select(tabId);
                    },
                    componentId: "chat.right_rail.workspace_tabs",
                    scope: conversationId,
                    onReorder: (activeKey, overKey) => {
                      const order = reorderStripEntries(reorderEntries, activeKey, overKey);
                      if (order) store.reorder(order);
                    },
                    pinsStayPut: pinsStayPutIn(stripWidth.areaWidth),
                    entries: tabs
                      .map((tab, index): WorkspaceSidebarStripEntry | null => {
                        const selected = tab.id === selectedTabId;
                        if (isUnknownTab(tab)) {
                          return {
                            key: tab.id,
                            tab: (
                              <StripTab
                                tabId={tab.id}
                                index={index}
                                title={UNSUPPORTED_TAB_TITLE}
                                selected={selected}
                                closable
                                onClose={(source) => closeTab(tab.id, source)}
                                width={stripWidth.width}
                                onKeyDown={(event) => actions.handleTabKeyDown(event, tab.id)}
                                menu={null}
                              />
                            ),
                          };
                        }
                        const title = titleOf(tab);
                        // The × is hidden where the tab can't close, not left as a dead control.
                        const closable = closeRefusalFor(tab) === undefined;
                        const pinned = isPinnedTab(tab);
                        return {
                          key: tab.id,
                          pinned,
                          tab: (
                            <StripTab
                              tabId={tab.id}
                              index={index}
                              title={title}
                              pinned={pinned}
                              unseen={tab.unseen === true || tab.attention !== undefined}
                              selected={selected}
                              closable={closable}
                              onClose={(source) => closeTab(tab.id, source)}
                              width={stripWidth.width}
                              onKeyDown={(event) => actions.handleTabKeyDown(event, tab.id)}
                              menu={
                                <StripTabMenu
                                  items={actions.menuItemsFor(tab.id, index, title.label, closable)}
                                />
                              }
                            />
                          ),
                        };
                      })
                      .filter((entry) => entry !== null),
                  }
            }
            placeholder={<span className="px-1 text-ui text-muted-foreground">Workspace</span>}
            afterTabs={
              // Below 160px "+" would run under Full screen, so it gives way; ⌘K and its chord still open a tab.
              !pending && (
                <div className="contents @max-[160px]/rail:hidden">
                  <NewTabButton onClick={newTab} />
                </div>
              )
            }
            trailing={
              <>
                {!inDrawer && (
                  <WorkspaceTabTooltip
                    label={fullScreenCommand?.menuLabel ?? "Full screen"}
                    shortcut={shortcutKeys("workspace.toggleFullScreen")}
                  >
                    <Button
                      type="button"
                      variant="ghost"
                      aria-label={fullScreenCommand?.menuLabel ?? "Full screen"}
                      aria-pressed={maximized}
                      onClick={onToggleMaximized}
                      disabled={pending}
                      size="icon-xs"
                      className="flex size-6 text-muted-foreground hover:text-foreground"
                    >
                      {maximized ? (
                        <MinimizeIcon className="size-4" />
                      ) : (
                        <MaximizeIcon className="size-4" />
                      )}
                    </Button>
                  </WorkspaceTabTooltip>
                )}
                {onHide && (
                  <WorkspaceTabTooltip
                    label="Collapse right panel"
                    shortcut={shortcutKeys("sidebar.right")}
                  >
                    <Button
                      type="button"
                      variant="ghost"
                      aria-label="Collapse right panel"
                      aria-keyshortcuts={shortcutAriaKeys("sidebar.right")}
                      onClick={onHide}
                      size="icon-xs"
                      // A narrow strip leaves room to "+", as the header's toggle beside it does the same.
                      className="flex size-6 text-muted-foreground hover:text-foreground @max-[240px]/rail:hidden"
                    >
                      <XIcon className="size-4" />
                    </Button>
                  </WorkspaceTabTooltip>
                )}
              </>
            }
            banner={
              selectedTab?.attention && (
                <AttentionBar
                  label={titleOf({ ...selectedTab, place: selectedTab.attention.place }).label}
                  onShow={() => void store.showAttention(selectedTab.id)}
                  onDismiss={() => store.dismissAttention(selectedTab.id)}
                />
              )
            }
            contentRef={contentRef}
          >
            {pending ? (
              <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 text-muted-foreground">
                <Spinner />
                <span className="text-ui">Starting workspace…</span>
              </div>
            ) : (
              <>
                {mountedTabs.map((tab) => {
                  const selected = tab.id === selectedTabId;
                  return (
                    <WorkspaceSidebarTabPanel key={tab.id} tabId={tab.id} selected={selected}>
                      {renderTabContent(tab, selected)}
                    </WorkspaceSidebarTabPanel>
                  );
                })}
                {selectedUnknown && (
                  <UnsupportedTabContent onClose={() => closeTab(selectedUnknown.id)} />
                )}
              </>
            )}
          </WorkspaceSidebarFrame>
        </div>
      </aside>
    </>
  );
}

function pagePlace(page: DefaultWorkspaceTab): WorkspacePlace {
  switch (page) {
    case "files":
      return { kind: "files" };
    case "changes":
      return { kind: "changes" };
    case "github":
      return { kind: "pulls" };
    case "subagents":
      return { kind: "agents" };
    case "tasks":
      return { kind: "tasks" };
  }
}

function AttentionBar({
  label,
  onShow,
  onDismiss,
}: {
  label: string;
  onShow: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      role="status"
      className="flex shrink-0 items-center gap-2 border-b border-border bg-muted/40 px-3 py-1.5 text-ui"
    >
      <span className="min-w-0 flex-1 truncate">An agent opened {label} here.</span>
      <Button type="button" size="xs" variant="outline" onClick={onShow}>
        Show
      </Button>
      <Button type="button" size="xs" variant="ghost" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  );
}

/** Names a shell tab showing one shell; a tab rebound to another shell is a new entry. */
function attachedShellKey(tabId: TabId, terminalKey: string): string {
  return `${tabId}\u0000${terminalKey}`;
}

function Unavailable({ text }: { text: string }) {
  return (
    <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-muted-foreground">
      {text}
    </div>
  );
}

export const WorkspaceTabsPanel = memo(WorkspaceTabsPanelImpl);
