import {
  type ComponentType,
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type RefObject,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  CheckIcon,
  CheckIcon as CheckMarkIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ClockIcon,
  CircleStopIcon,
  FolderIcon,
  FolderInputIcon,
  FolderMinusIcon,
  FolderOpenIcon,
  GitBranchIcon,
  InboxIcon,
  ListChecksIcon,
  ListFilterIcon,
  LaptopIcon,
  Loader2Icon,
  MailIcon,
  MessageCircleDashedIcon,
  Maximize2Icon,
  Minimize2Icon,
  MoreHorizontalIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  SearchIcon,
  Settings2Icon,
  ShareIcon,
  SmilePlusIcon,
  SquareIcon,
  SquareCheckIcon,
  SquarePenIcon,
  Trash2Icon,
  WalletIcon,
  XIcon,
} from "lucide-react";
import {
  DndContext,
  DragOverlay,
  type DragEndEvent,
  type DragStartEvent,
  MeasuringStrategy,
  MouseSensor,
  pointerWithin,
  TouchSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "@/lib/routing";
import { SidebarHeaderActions, SidebarSettingsButton } from "./SidebarHeaderActions";
import omnigentWordmark from "@/assets/omnigent-wordmark.svg";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  type Conversation,
  type PinnedConversationsResult,
  useArchiveConversation,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useBulkMoveToProject,
  useProjects,
  useProjectSessions,
  useConversations,
  useLeaveSession,
  useMoveToProject,
  useDeleteProject,
  useRenameProject,
  useProjectConfig,
  useUpdateProjectConfig,
  PROJECT_LABEL_KEY,
  PINNED_CONVERSATIONS_KEY,
  usePinnedConversations,
  useTogglePinnedConversation,
  setConversationPinned,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
} from "@/hooks/useConversations";
import { useHosts, type Host } from "@/hooks/useHosts";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled, isSingleUserMode, sandboxOptionLabel } from "@/lib/capabilities";
import { useBranding } from "@/lib/branding";
import { relativeTime } from "@/lib/relativeTime";
import { USER_SESSION_TITLE_MAX_CHARS } from "@/lib/sessionTitles";
import { showToast } from "@/components/ui/toast";
import { PermissionsModal } from "@/components/PermissionsModal";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { ProjectRowIcon } from "./ProjectPicker";
import { EmojiPicker } from "@/components/ProjectIconPicker";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useActiveRootSessionId } from "@/hooks/useSession";
import { useCommentInbox } from "@/hooks/useCommentInbox";
import { sumPendingApprovals } from "@/lib/inbox";
import { isSessionStoppable } from "@/lib/sessionStop";
import { getCurrentUserId, resolveIdentity } from "@/lib/identity";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { useHasSessionDraft } from "@/lib/sessionDrafts";
import { useOptimisticTitle } from "@/lib/optimisticTitles";
import { getSessionState, type SessionState } from "@/hooks/useSessionState";
import { useChatStore } from "@/store/chatStore";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  markConversationUnread,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { cn } from "@/lib/utils";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizableSidebar } from "@/hooks/useResizableSidebar";
import { useSessionSwitchHotkey } from "@/hooks/useSessionSwitchHotkey";
import { usePinnedSessionHotkeys } from "@/hooks/usePinnedSessionHotkeys";
import { isCurrentServerLocal } from "@/lib/serverOrigin";
import {
  type SessionFilter,
  readSessionFilter,
  writeSessionFilter,
} from "@/lib/sessionFilterPreferences";
import { NewProjectButton } from "./NewProjectButton";
import { SettingsSidebarBody, useSettingsRoute, useTrackSettingsReturn } from "./settingsNav";
import {
  type ActiveChatOverride,
  clearLegacyPinnedConversationIds,
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  computeNextActiveOverride,
  conversationDisplayLabel,
  dedupeConversationsById,
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
  orderByPinnedTimestamp,
  readPinnedConversationIds,
  resolveSidebarDrop,
  type SidebarDropTarget,
  sortByUpdatedAtDesc,
  writeLegacyPinnedConversationIds,
} from "./sidebarNav";
import { SidebarServerPicker } from "./SidebarServerPicker";
import { SIDEBAR_ROW } from "./sidebarStyles";

// Positioning for a row's trailing session-state badge. Anchored at the row's
// right-1 edge in every viewport: on desktop it fades on hover so the pin +
// kebab take its place; on mobile those controls are gone, so the badge simply
// holds the right edge.
const SESSION_STATE_SLOT_CLASS =
  "-translate-y-1/2 pointer-events-none absolute top-1/2 right-1 flex h-5 items-center transition-opacity md:group-hover:opacity-0 md:group-has-[:focus-visible]:opacity-0 md:group-has-[[aria-expanded=true]]:opacity-0";

// Small markers (running/starting/unseen dot, or the draft pencil when there's
// no session state) get a fixed size-6 centered box so their glyph lands 16px
// from the right edge — lining up vertically with the desktop kebab and the
// size-6 section-header icons above (the marker column) in every viewport. The
// "awaiting" pill keeps its natural width — a fixed box would clip its "Needs
// response" label.
function isDotMarker(state: SessionState | null): boolean {
  return state === null || state.kind !== "awaiting";
}
const SESSION_STATE_DOT_SLOT_CLASS = "w-6 justify-center";

// Match the Settings sidebar's ghost-button hover treatment across every home
// sidebar row.
const SIDEBAR_HOVER_HIGHLIGHT = "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50";
// Keep a project-folder row highlighted while its actions menu is open (kebab
// or header context menu, both flagged `data-state=open` inside `group/header`).
// A right-click opens the context menu in a portal, dropping `:hover` from the
// header, so the hover highlight alone would leave the row un-highlighted.
const SIDEBAR_OPEN_MENU_HIGHLIGHT =
  "group-has-[[data-state=open]]/header:bg-muted group-has-[[data-state=open]]/header:text-foreground dark:group-has-[[data-state=open]]/header:bg-muted/50";
// Active highlight also wins on hover so active items don't lose their
// background and flash when the mouse enters them.
const SIDEBAR_ACTIVE_HIGHLIGHT =
  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)] hover:bg-[var(--sidebar-active)] hover:text-[var(--sidebar-active-foreground)] dark:hover:bg-[var(--sidebar-active)] dark:hover:text-[var(--sidebar-active-foreground)]";
const DROP_TARGET_HIGHLIGHT = SIDEBAR_ACTIVE_HIGHLIGHT;

// Maps a first-class project id → its name, provided once at the list level so
// each row resolves its ``project_id`` to a folder name without its own
// ``useProjects()`` subscription. Keeps row renders O(1) and avoids spinning up
// a query observer per row (which would also re-run on every project mutation).
const ProjectNamesContext = createContext<Map<string, string>>(new Map());
const HostsByIdContext = createContext<ReadonlyMap<string, Host>>(new Map());
// Rows report an in-progress inline-rename edit here so ConversationList can
// hold the sort order for the edit's whole duration — the pointer often
// leaves the list while typing, and a reorder then would shuffle rows around
// the open input (and can even blur it mid-edit, committing a half-typed
// title). See the order-freeze block in ConversationList.
const RowEditHoldContext = createContext<(id: string, editing: boolean) => void>(() => {});

function SidebarRowDataProvider({
  projectNamesById,
  hostsById,
  children,
}: {
  projectNamesById: Map<string, string>;
  hostsById: ReadonlyMap<string, Host>;
  children: ReactNode;
}) {
  return (
    <ProjectNamesContext.Provider value={projectNamesById}>
      <HostsByIdContext.Provider value={hostsById}>{children}</HostsByIdContext.Provider>
    </ProjectNamesContext.Provider>
  );
}

/**
 * Which slice of sessions the sidebar shows. ``"mine"``/``"shared"`` split by
 * ownership (see :func:`isOwnedByViewer`); ``"archived"`` is the only slice
 * that includes archived sessions. The vocabulary lives with the persistence
 * helpers, which validate a stored value against it.
 */
type SidebarTab = SessionFilter;

const SIDEBAR_FILTERS: { value: SidebarTab; label: string }[] = [
  { value: "all", label: "All sessions" },
  { value: "mine", label: "My sessions" },
  { value: "shared", label: "Shared sessions" },
  { value: "archived", label: "Archived sessions" },
];

// Shown in place of the list when a filter matches nothing.
const SIDEBAR_FILTER_EMPTY: Record<SidebarTab, string> = {
  all: "No sessions",
  mine: "No sessions",
  shared: "No sessions",
  archived: "No sessions",
};

// Bulk-selection targets either the flat "Sessions" list or the sessions
// nested inside project folders; the active scope decides which rows show
// checkboxes and where the bulk-action bar renders.
type SelectionScope = "sessions" | "projects";

interface SidebarProps {
  open: boolean;
  onClose: () => void;
  /**
   * Pin a peeking sidebar fully open (the in-sidebar toggle shown while
   * peeking). Optional (defaults to a no-op) so the sidebar renders standalone
   * in tests.
   */
  onOpen?: () => void;
  /**
   * Live open fraction (0 = closed, 1 = open) while the iOS shell's left-edge
   * swipe is dragging the sidebar; `null` when not dragging. When set, the
   * mobile overlay tracks it directly (transition suppressed) so the drawer
   * follows the finger; on release the parent clears it and toggles `open`,
   * letting the CSS transition animate to the resting state.
   */
  dragProgress?: number | null;
  /**
   * Open the global command palette (⌘K). The sidebar's "Search" button routes
   * here rather than filtering inline: session search (title + chat content)
   * lives in the palette, which the box now doubles as an entry point for.
   * Optional (defaults to a no-op) so the sidebar renders standalone in tests.
   */
  onOpenSearch?: () => void;
  /**
   * Whether the sidebar is peeking.
   */
  peek?: boolean;
}

/**
 * Which top-level nav button (New session / Inbox) is active for the current
 * route.
 *
 * The inbox route has no param to key off, and the sidebar is basename-agnostic
 * (in embedded mode the routing seam rebases `to="/inbox"` → `${basename}/inbox`
 * behind its back), so `useMatch` / `NavLink` can't be used without knowing the
 * mount path. Instead compare the active route's last non-empty path segment,
 * which is `inbox` in both standalone and embedded modes. Conversation ids are
 * `conv_…`-prefixed, so a chat route's leaf can never collide with `inbox`.
 */
function useActiveNavItem(): {
  isNewChatPage: boolean;
  isInboxPage: boolean;
  isTasksPage: boolean;
  isUsagePage: boolean;
  newSessionProjectName: string | null;
} {
  const { conversationId: activeConversationId } = useParams<{ conversationId: string }>();
  const location = useLocation();
  const leaf = location.pathname.split("/").filter(Boolean).at(-1);
  const isInboxPage = leaf === "inbox";
  const isTasksPage = leaf === "tasks";
  const isUsagePage = leaf === "usage";
  const isNewSessionRoute =
    activeConversationId == null && !isInboxPage && !isTasksPage && !isUsagePage;
  const requestedProject = isNewSessionRoute
    ? new URLSearchParams(location.search).get("project")
    : null;
  const newSessionProjectName = requestedProject || null;
  // Exclude inbox/tasks/usage: they also have no `:conversationId`, so they
  // would otherwise light up the "New session" button. A project-prefilled
  // new session belongs to that project row instead of the global nav item.
  const isNewChatPage = isNewSessionRoute && newSessionProjectName == null;
  return { isNewChatPage, isInboxPage, isTasksPage, isUsagePage, newSessionProjectName };
}

/**
 * Sidebar — brand mark, "New chat" button, conversations list.
 *
 * Responsive layout (mobile overlay vs desktop push) — see AppShell for
 * the layout side of the contract. Auto-close behavior is also
 * viewport-conditional:
 *
 *   - **Mobile**: navigation actions (New chat, conversation rows)
 *     close the sidebar. The sidebar covers the chat as a full-screen
 *     overlay, so dismissing on action is what reveals the new
 *     destination.
 *   - **Desktop**: navigation actions do NOT close. Only the X button
 *     in the brand row dismisses. Pushing chat content aside to read
 *     scrollback is fine; users typically want the conversations list
 *     to stay visible while they switch around.
 */
/** Toast body shown after archiving a session — links to its new home. */
function ArchivedToast() {
  return (
    <span>
      View archived sessions in{" "}
      <Link to="/settings/archived" className="font-medium text-primary hover:underline">
        Settings
      </Link>
    </span>
  );
}

/**
 * Compute the set of IDs to add for a shift-click range selection.
 * Returns null when the range can't be computed (missing anchor or id).
 */
export function computeShiftSelectRange(
  visibleIds: readonly string[],
  anchorId: string,
  targetId: string,
): string[] | null {
  const anchorIdx = visibleIds.indexOf(anchorId);
  const targetIdx = visibleIds.indexOf(targetId);
  if (anchorIdx === -1 || targetIdx === -1) return null;
  const [start, end] = anchorIdx < targetIdx ? [anchorIdx, targetIdx] : [targetIdx, anchorIdx];
  return visibleIds.slice(start, end + 1);
}

/** Fire the post-archive toast. Hoisted so it isn't a render-scoped closure. */
function showArchivedToast() {
  showToast(<ArchivedToast />);
}

/** Stable empty array for the pinned-conversations fallback (referential
    equality keeps dependent memos from re-firing while the query loads). */
const EMPTY_CONVERSATIONS: Conversation[] = [];

/**
 * One-time migration of localStorage pins to server-side labels.
 *
 * Pins used to live only in `localStorage` under
 * `PINNED_CONVERSATION_IDS_STORAGE_KEY`. Now they're an `omnigent.pinned`
 * session label so they follow the user across devices. On the first mount
 * after this ships, push any still-local pins the server doesn't already know
 * about (as the label) so no one loses their existing pins.
 *
 * Runs only when `filterHonored` is true — i.e. the server actually applied
 * `?pinned=true`, so it can store server-side pins. A pre-upgrade server that
 * predates this feature ignores the param and returns an unfiltered page;
 * migrating against it would PATCH pins under a key that server can't
 * per-user-scope AND clear the legacy key, so after the eventual server upgrade
 * every pin would read as unpinned. Gating on `filterHonored` keeps the
 * migration inert (localStorage untouched, pins still render via the union in
 * the caller) until the server can honor it — so a UI-before-server upgrade is
 * safe.
 *
 * A legacy id is only dropped from localStorage once its server write is
 * confirmed; anything unwritten (failed, offline, or not-yet-run because the
 * server can't store pins) stays so the next load retries. Runs the writes
 * directly rather than through the mutation hook: this fires once before any
 * user interaction, and it patches the pinned-list cache itself with the
 * confirmed rows — the same cache-patch (not invalidate) strategy
 * `useTogglePinnedConversation` uses, since the `?pinned=true` index lags these
 * writes.
 *
 * @param serverPinnedIds - Ids the server already reports as pinned.
 * @param pinnedLoaded - Whether the server pinned query has settled.
 * @param filterHonored - Whether the server applied the `?pinned=true` filter.
 */
export function useMigrateLocalPinsToServer(
  serverPinnedIds: Set<string>,
  pinnedLoaded: boolean,
  filterHonored: boolean,
): void {
  const queryClient = useQueryClient();
  const migratedRef = useRef(false);
  useEffect(() => {
    // Don't migrate until the query settled AND the server proved it honors the
    // pinned filter — an old server ignores it, and migrating there wipes local
    // pins. Leave `migratedRef` false so a later load (post server upgrade)
    // still runs the migration.
    if (!pinnedLoaded || !filterHonored || migratedRef.current) return;
    migratedRef.current = true;
    const legacyIds = readPinnedConversationIds();
    const toMigrate = legacyIds.filter((id) => !serverPinnedIds.has(id));
    // Ids the server already owns can be dropped from the legacy key right away;
    // ids still to migrate stay until their write succeeds (below), so a failed
    // or offline write retries next load instead of losing the pin.
    if (toMigrate.length === 0) {
      clearLegacyPinnedConversationIds();
      return;
    }
    writeLegacyPinnedConversationIds(toMigrate);
    void (async () => {
      // Legacy localStorage kept pins most-recently-pinned-first, so preserve
      // that order by synthesizing descending pin timestamps: the oldest pin
      // (last in the list) gets the smallest value and stays at the top of the
      // Pinned section, matching the pre-migration ordering.
      const now = Date.now();
      const results = await Promise.all(
        toMigrate.map((id, i) =>
          setConversationPinned(id, true, now - i)
            .then((conv) => ({ id, conv }))
            .catch(() => ({ id, conv: null as Conversation | null })),
        ),
      );
      // Keep only the ids whose write failed in the legacy key, so the next
      // load retries them; drop the succeeded ones (now server-owned).
      const failedIds = results.filter((r) => r.conv === null).map((r) => r.id);
      writeLegacyPinnedConversationIds(failedIds);
      // Patch the pinned-list cache with the confirmed rows rather than
      // invalidating — the `?pinned=true` index lags these writes, so a refetch
      // here would momentarily drop the just-migrated pins.
      const rows = results.map((r) => r.conv).filter((c): c is Conversation => c != null);
      if (rows.length > 0) {
        queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (old) => {
          const ids = new Set(rows.map((c) => c.id));
          const prev = old ?? { conversations: [], filterHonored: true };
          return {
            ...prev,
            conversations: [...prev.conversations.filter((c) => !ids.has(c.id)), ...rows],
          };
        });
      }
    })();
    // Re-run when the query settles or the filter starts being honored (post
    // server upgrade); the ref guard prevents re-entry once it actually runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinnedLoaded, filterHonored]);
}

export function Sidebar({
  open,
  onClose,
  onOpen,
  dragProgress = null,
  onOpenSearch,
  peek,
}: SidebarProps) {
  const branding = useBranding();
  const serverInfo = useServerInfo();
  const usagePageEnabled = isFeatureEnabled(serverInfo, "usage_page");
  const [selectionMode, setSelectionMode] = useState(false);
  // Which rows the current selection targets: the flat "Sessions" list, or the
  // sessions nested inside project folders. Set when selection mode is entered
  // (from the Sessions header or the Projects header kebab, respectively).
  const [selectionScope, setSelectionScope] = useState<SelectionScope>("sessions");
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // A loopback-only server has one user, so "Shared" is meaningless there —
  // the filter menu drops that option. Mirrors AppShell's `shareDisabled`.
  // Read before the filter state, which validates a stored "shared" against it.
  const multiUser = !isCurrentServerLocal();
  // Active filter from the Sessions heading's menu, seeded from the persisted
  // preference so a reload keeps the slice the viewer was last on.
  const [activeTab, setActiveTab] = useState<SidebarTab>(() => readSessionFilter(multiUser));

  const lastSelectedIdRef = useRef<string | null>(null);
  const getVisibleIdsRef = useRef<() => string[]>(() => []);

  const toggleSelected = useCallback((id: string, shiftKey?: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (shiftKey && lastSelectedIdRef.current != null) {
        const range = computeShiftSelectRange(
          getVisibleIdsRef.current(),
          lastSelectedIdRef.current,
          id,
        );
        if (range) {
          for (const rid of range) next.add(rid);
          return next;
        }
      }
      if (next.has(id)) next.delete(id);
      else next.add(id);
      lastSelectedIdRef.current = id;
      return next;
    });
  }, []);

  const deselectAll = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false);
    setSelectionScope("sessions");
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
  }, []);

  // Enter selection mode targeting a given scope; clear any prior selection so
  // rows from the previous scope don't linger.
  const enterSelectionMode = useCallback((scope: SelectionScope) => {
    setSelectionScope(scope);
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
    setSelectionMode(true);
  }, []);

  // Switch the visible scope tab. Selection is a single global set while the
  // tabs show disjoint, ownership-scoped slices, so leaving selection mode on
  // switch keeps the bulk-action count honest with the visible tab (the viewer
  // re-enters per tab) instead of carrying stale rows across. Every path that
  // changes the tab must go through here — not a bare setActiveTab — or the
  // selection cleanup and the persisted preference are skipped (e.g. the
  // "New session" snap-back below).
  const switchTab = useCallback(
    (tab: SidebarTab) => {
      if (selectionMode) exitSelectionMode();
      setActiveTab(tab);
      writeSessionFilter(tab);
    },
    [selectionMode, exitSelectionMode],
  );

  // One paginated session list — sessions are no longer split by
  // connection state, so the sidebar fetches a single undifferentiated
  // list. Archived sessions are included (`includeArchived: true`) and
  // peeled into their own "Archived" section at the bottom of the list.
  // Session search now lives in the command palette (the "Search" button
  // below), so the sidebar list itself is unfiltered.
  const conversationsQuery = useConversations("", true, {
    reconcileWhileConnected: true,
  });

  // The scrollable list container — used as the IntersectionObserver root for
  // infinite scroll (auto-loading the next page as the sentinel nears view).
  const scrollContainerRef = useRef<HTMLElement>(null);

  // Inbox badge — total approval prompts across loaded rows. Same
  // `pending_elicitations_count` the per-row "awaiting" hand badge
  // reads (live via WS /v1/sessions/updates), just summed.
  const loadedRows = useMemo(
    () => (conversationsQuery.data?.pages ?? []).flatMap((page) => page.data),
    [conversationsQuery.data],
  );
  const pendingApprovals = useMemo(() => sumPendingApprovals(loadedRows), [loadedRows]);
  // Plus unseen file comments — the badge counts everything the Inbox
  // page lists. Comment queries are shared with the page/FileViewer
  // (same ["comments", id] keys), so this adds no duplicate fetches.
  const unseenComments = useCommentInbox(loadedRows).items.length;
  const inboxCount = pendingApprovals + unseenComments;

  // Click handler for conversation-row Links in the sidebar. The Link
  // handles navigation natively, so cmd/ctrl/middle-click opens new
  // tabs. We still want to close on mobile after a plain primary click,
  // but NOT for modifier/middle clicks that open a new tab — those
  // don't change the current view.
  function onNavClick(e: MouseEvent<HTMLAnchorElement>) {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (isMobileViewport()) onClose();
  }

  // Which top-level nav button to highlight for the current route.
  const { isNewChatPage, isInboxPage, isTasksPage, isUsagePage, newSessionProjectName } =
    useActiveNavItem();

  // On /settings the card keeps its chrome but swaps the conversation list
  // for the settings section nav (see settingsNav.tsx) — entering settings
  // shouldn't replace the whole sidebar.
  const { inSettings } = useSettingsRoute();
  // Remember the pre-settings location so the Back row returns to the
  // conversation the user was viewing, not the home page. Tracked here since
  // the sidebar stays mounted across the transition into settings.
  useTrackSettingsReturn();

  // Pins are stored on the server as an `omnigent.pinned` session label, so
  // they follow the user across devices. `usePinnedConversations` is the
  // authoritative pinned set (independent of the paginated window); the toggle
  // mutation flips the label and refreshes that query.
  const { data: pinnedData, isSuccess: pinnedLoaded } = usePinnedConversations();
  // Stable empty fallback so downstream memos don't re-fire on every render
  // while the query is still loading (`pinnedData` undefined).
  const pinnedConversations = useMemo(
    () => pinnedData?.conversations ?? EMPTY_CONVERSATIONS,
    [pinnedData],
  );
  const pinnedFilterHonored = pinnedData?.filterHonored ?? false;
  // Membership is the union of the server's pinned rows and any pins still in
  // the legacy localStorage key — so a not-yet-migrated pin (server too old, or
  // a migration write that hasn't landed) keeps showing in the Pinned section
  // instead of vanishing. The union collapses to just the server set once the
  // migration clears the legacy key. Ordering/rows still come from
  // `pinnedConversations` where available; a legacy-only id renders from the
  // loaded list rows the grouping already has.
  //
  // Caveat: a legacy-only id whose session is OUTSIDE the currently-loaded
  // paginated window has no backing row, so the id is in the pinned set but may
  // not render a row until it's loaded. This is window-scoped and transient —
  // against a new server the migration promotes the id to a real server pinned
  // row (which carries its own row) on the same or next load.
  const pinnedConversationIds = useMemo(() => {
    const ids = pinnedConversations.map((c) => c.id);
    const seen = new Set(ids);
    for (const id of readPinnedConversationIds()) if (!seen.has(id)) ids.push(id);
    return ids;
    // `pinnedLoaded` isn't read but is a dep on purpose: it re-reads the legacy
    // key after the migration (gated on the query settling) mutates it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinnedConversations, pinnedLoaded]);
  const togglePinnedMutation = useTogglePinnedConversation();
  const pinnedIdSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  // The migration compares the legacy key against what the SERVER already owns
  // (not the union — a legacy-only id must still count as "to migrate").
  const serverPinnedIdSet = useMemo(
    () => new Set(pinnedConversations.map((c) => c.id)),
    [pinnedConversations],
  );
  const togglePinnedConversation = useCallback(
    (conversationId: string) => {
      togglePinnedMutation.mutate({
        id: conversationId,
        pinned: !pinnedIdSet.has(conversationId),
      });
    },
    [togglePinnedMutation, pinnedIdSet],
  );

  // One-time migration: pins used to live only in localStorage. Push any
  // still-local pins up to the server (as the `omnigent.pinned` label) the
  // first time this build runs, so no one loses their existing pins, then
  // clear the legacy key so this runs at most once.
  useMigrateLocalPinsToServer(serverPinnedIdSet, pinnedLoaded, pinnedFilterHonored);

  // Desktop-only drag-to-resize, mirroring the right rail. The width is
  // exposed as a CSS variable consumed by the ``md:w-[var(--sidebar-width)]``
  // class so it only applies on desktop — on mobile the sidebar is a
  // full-screen overlay (``fixed inset-0``) and the variable is ignored.
  const { width: sidebarWidth, handleProps: resizeHandleProps } = useResizableSidebar();

  // While the iOS edge-swipe is dragging, the overlay is on-screen and
  // interactive even though `open` hasn't flipped yet — treat a live drag as
  // visually open so it isn't `inert`/`aria-hidden` mid-gesture.
  const dragging = dragProgress != null;
  const effectiveOpen = open || dragging || peek;

  // While peeking, leaving the card closes it after a short grace period;
  // re-entering before that fires cancels the close so a wobble doesn't
  // dismiss it.
  const peekCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelPeekClose = useCallback(() => {
    if (peekCloseTimer.current) {
      clearTimeout(peekCloseTimer.current);
      peekCloseTimer.current = null;
    }
  }, []);
  useEffect(() => cancelPeekClose, [cancelPeekClose]);

  return (
    <>
      {/* Mobile: the drawer stops short of the right edge, leaving a strip of
      the chat visible; this scrim covers that strip so tapping it dismisses the
      drawer — the gesture that replaced the collapse icon. Full-bleed and
      behind the drawer (z-45 vs z-50), so only the strip is actually reachable.
      Tracks the finger during an edge-swipe drag.

      A labeled <button>, not a bare div: with the collapse toggle gone on
      mobile this is the drawer's only non-navigational way out, so it has to be
      reachable by keyboard and announced by a screen reader, not just findable
      by sighted users with a pointer. Parked, it leaves the tab order and the
      a11y tree via tabIndex/aria-hidden rather than the `inert` the drawer
      uses: React 18 doesn't know `inert` as a boolean attribute and drops it,
      so a focusable control relying on it would stay tabbable while closed.

      z-45, not z-40: chat chrome also sits at z-40 (ChatPage's jump-to-top
      pill) and renders after the sidebar, so a same-z peer that ever takes
      pointer events would win the tie inside the exposed strip and swallow the
      dismiss. Strictly between chat chrome and the drawer's z-50. */}
      {!inSettings && (
        <button
          type="button"
          data-testid="sidebar-scrim"
          aria-label="Close sidebar"
          onClick={onClose}
          tabIndex={effectiveOpen ? 0 : -1}
          aria-hidden={!effectiveOpen}
          className={cn(
            "fixed inset-0 z-[45] bg-black/25 transition-opacity duration-200 ease-out md:hidden",
            effectiveOpen ? "opacity-100" : "pointer-events-none opacity-0",
          )}
          style={dragging ? { opacity: dragProgress, transition: "none" } : undefined}
        />
      )}
      <aside
        aria-label="Conversations"
        onPointerEnter={cancelPeekClose}
        onPointerLeave={() => {
          if (!peek) return;
          cancelPeekClose();
          // Defer closing if any context menu is open
          const tryClose = () => {
            if (document.querySelector('[role="menu"][data-state="open"]')) {
              peekCloseTimer.current = setTimeout(tryClose, 200);
              return;
            }
            onClose();
          };
          peekCloseTimer.current = setTimeout(tryClose, 200);
        }}
        className={cn(
          // Base: bg + flex column. No transition — expand/collapse snaps
          // instantly (animating the width also lagged drag-to-resize).
          // conversations-sidebar only matters under the macOS Electron
          // shell, where it pushes the card below the traffic lights
          // (see the [data-electron-mac] rules in index.css).
          "conversations-sidebar flex flex-col bg-card md:select-none",
          // Mobile (default): a fixed drawer that slides in via translate-x
          // and stops short of the right edge, leaving a tappable strip of the
          // chat behind it. The floating-card treatment below is desktop-only.
          // bg-card-solid (opaque): the overlay sits on top of the chat, and
          // WebKit drops the glass rule's backdrop-filter once a Radix popper
          // opens (and never repaints it), letting the chat bleed through the
          // 60%-alpha glass --card. Desktop keeps the translucent bg-card —
          // there the sidebar pushes content aside, so nothing sits behind it.
          "max-md:bg-card-solid",
          // `max-md:right-14` is the ChatGPT-style peek strip: the drawer covers
          // most of the phone but stops short of the right edge so the chat stays
          // visible (and tappable through the scrim) behind it.
          "fixed inset-0 z-50",
          // The settings nav takes the sidebar over and its "Back" row is the
          // only way out, so there the drawer stays full-bleed (and gets no
          // dismiss scrim) rather than offering a tap that strands the user.
          !inSettings && "max-md:right-14",
          // Mobile only: animate the slide so the iOS edge-swipe settles
          // smoothly on release. Suppressed inline while a drag is live (the
          // overlay must track the finger 1:1). Scoped to transform so it can't
          // re-introduce the width-animation lag the base comment warns about,
          // and gated to mobile so the desktop floating card is unaffected.
          "max-md:transition-transform max-md:duration-200 max-md:ease-out",
          effectiveOpen ? "translate-x-0" : "-translate-x-full",
          // Desktop: a full-height panel flush to the window edge, carrying
          // the brand gradient canvas (see html:not(.dark) .conversations-sidebar
          // in index.css) and separated from the white content by a right
          // divider — no outer margin or rounding. Width (the user-resizable
          // variable) animates →0 to push main; when closed the border
          // collapses too so nothing lingers.
          "md:translate-x-0 md:overflow-hidden",
          // Normal desktop flow: relative panel that pushes main. Suppressed while
          // peeking so its `md:inset-auto`/`md:relative` don't override the
          // floating-card positioning below (same `md:` layer, source order wins).
          !peek && "md:relative md:inset-auto",
          open || peek ? "md:m-0 md:w-[var(--sidebar-width)] " : "md:m-0 md:w-0 md:border-0",
          // Peek: float as a card 4px off the viewport edge (capped at 300px wide),
          // ringed and shadowed, sliding+fading in from the left so it reads as an
          // overlay rather than a push.
          peek &&
            "is-peek md:absolute md:inset-2 p-0 md:max-w-[400px] ring-1 ring-border rounded-xl md:shadow-xl animate-in fade-in slide-in-from-left-4 duration-200 ease-out",
        )}
        style={
          {
            "--sidebar-width": `${sidebarWidth}px`,
            // Track the finger: map the 0→1 open fraction to translateX
            // -100%→0% and kill the transition so it follows the drag exactly.
            ...(dragging
              ? { transform: `translateX(${(dragProgress - 1) * 100}%)`, transition: "none" }
              : null),
          } as CSSProperties
        }
        // Hide from the accessibility tree when closed so screen readers
        // don't see the empty-state contents while focus is elsewhere.
        aria-hidden={!effectiveOpen}
        data-collapsed={!effectiveOpen || undefined}
        // Match the keyboard-focus story: when closed, the sidebar's
        // children shouldn't receive tabs.
        inert={!effectiveOpen}
      >
        {/* Right-edge resize handle (desktop only), mirroring the right rail's
          left-edge handle. Hidden on mobile, where the sidebar is a
          full-screen overlay with no resize affordance; the parent's ``inert``
          when closed also keeps it from being draggable while collapsed.
          Hidden while peeking too — the peek card is a fixed-width flyout, not
          a resizable panel. */}
        {!peek && (
          <div
            {...resizeHandleProps}
            className="absolute inset-y-0 right-0 z-10 hidden w-1 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50 md:block"
          />
        )}
        {inSettings ? (
          <SettingsSidebarBody onNavClick={onNavClick} />
        ) : (
          <>
            {/* sidebar-header-row is the hook for the macOS Electron shell, where
          this row shares the window's top strip with the traffic lights: the
          brand mark is dropped and the actions slide left to sit beside the
          window controls (see the [data-electron-mac] rules in index.css).
          Inert in a browser and on other platforms, which keep the row below. */}
            <div className="sidebar-header-row flex h-12 shrink-0 items-center justify-between pr-3 pl-4">
              {/* Brand mark doubles as the "home" affordance: clicking it
            returns to `/`, the new-session composer. Without this there
            is no way back to the landing composer once you're inside a
            session. Reuses onNavClick so a plain primary click closes
            the sidebar on mobile (where it's a full-screen overlay) but
            modifier/middle clicks still open `/` in a new tab. */}
              <Link
                to="/"
                onClick={onNavClick}
                data-testid="sidebar-brand"
                componentId="sidebar.home"
                className="sidebar-brand rounded-none transition-opacity duration-200 ease-[var(--ease-otto)] hover:opacity-70"
              >
                {branding.app_name ? (
                  <span className="text-[15px] font-semibold tracking-tight">
                    {branding.app_name}
                  </span>
                ) : (
                  <img
                    src={omnigentWordmark}
                    alt="Omnigent"
                    data-testid="sidebar-wordmark"
                    className="h-[15px] w-auto shrink-0 translate-y-px dark:invert"
                  />
                )}
              </Link>
              {/* On the macOS shell this copy is hidden and an identical cluster
            renders in the title-bar strip instead (see AppShell), so the icons
            keep their place when the sidebar collapses or peeks. Everywhere
            else this is the only copy. */}
              <SidebarHeaderActions
                expanded={!peek}
                // onOpen is optional (the sidebar renders standalone in tests), so
                // fall back to a no-op rather than widening the child's contract.
                onToggle={peek ? () => onOpen?.() : onClose}
                onOpenSearch={onOpenSearch}
              />
            </div>

            <div className="flex flex-col gap-0 px-2 pt-2 pb-0" data-testid="sidebar-primary-nav">
              {/* "New session" routes to the home composer ("/"), which now owns
            session creation end-to-end (host/workspace/worktree chips +
            send). Rendered as a Link so cmd/middle-click opens it in a new
            tab; onNavClick still closes the sidebar on a plain mobile tap. */}
              <Button
                asChild
                className={cn(
                  // px-2 + gap-2 puts the icon on the sidebar's left (red) column
                  // and the label on the label (blue) column — matching section
                  // headers and project folders. border-0 drops the Button base's
                  // transparent 1px border so the icon lands exactly on that
                  // column, flush with the Inbox row and folder rows.
                  SIDEBAR_ROW,
                  "w-full justify-start border-0 font-normal",
                  SIDEBAR_HOVER_HIGHLIGHT,
                  isNewChatPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                )}
                variant="ghost"
                data-testid="new-chat-button"
              >
                {/* New session always creates a session the viewer owns, which
              lands under "My sessions" — so snap the tab back there on click
              (the button stays visible on both tabs). */}
                <Link
                  to="/"
                  componentId="sidebar.new_chat"
                  onClick={(e) => {
                    switchTab("mine");
                    onNavClick(e);
                  }}
                >
                  <SquarePenIcon
                    className={cn(
                      "ui-icon",
                      isNewChatPage
                        ? "text-[var(--sidebar-active-foreground)]"
                        : "text-muted-foreground",
                    )}
                  />
                  New session
                </Link>
              </Button>
              {/* Keep Scheduled in the primary nav group with the same row treatment as New session. */}
              <Button
                asChild
                className={cn(
                  // Same shared nav-row construct as "New session" / "Inbox" so
                  // the active-pill, hover, insets, icon column, and text weight
                  // all match post-refactor.
                  SIDEBAR_ROW,
                  "w-full justify-start border-0 font-normal",
                  SIDEBAR_HOVER_HIGHLIGHT,
                  isTasksPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                )}
                variant="ghost"
                data-testid="scheduled-tasks-nav"
              >
                <Link to="/tasks" onClick={onNavClick} componentId="sidebar.tasks">
                  <ClockIcon
                    className={cn(
                      "ui-icon",
                      isTasksPage
                        ? "text-[var(--sidebar-active-foreground)]"
                        : "text-muted-foreground",
                    )}
                  />
                  Automations
                </Link>
              </Button>
              <Button
                asChild
                variant="ghost"
                className={cn(
                  SIDEBAR_ROW,
                  "w-full justify-start border-0 font-normal",
                  SIDEBAR_HOVER_HIGHLIGHT,
                  isInboxPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                )}
                data-testid="inbox-button"
              >
                <Link to="/inbox" onClick={onNavClick} componentId="sidebar.inbox">
                  <InboxIcon
                    className={cn(
                      "ui-icon",
                      isInboxPage
                        ? "text-[var(--sidebar-active-foreground)]"
                        : "text-muted-foreground",
                    )}
                  />
                  Inbox
                  {inboxCount > 0 && (
                    <span
                      aria-label={
                        inboxCount === 1
                          ? "1 inbox item waiting"
                          : `${inboxCount} inbox items waiting`
                      }
                      className={cn(
                        "ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-10 font-medium text-[var(--sidebar-active-foreground)] tabular-nums",
                        // The active Inbox row already paints the translucent
                        // --sidebar-active wash; repainting it on the nested
                        // badge would double-composite to a darker fill.
                        isInboxPage ? "bg-transparent" : "bg-[var(--sidebar-active)]",
                      )}
                    >
                      {inboxCount}
                    </span>
                  )}
                </Link>
              </Button>
              {usagePageEnabled && (
                <Button
                  asChild
                  variant="ghost"
                  className={cn(
                    SIDEBAR_ROW,
                    "w-full justify-start border-0 font-normal",
                    SIDEBAR_HOVER_HIGHLIGHT,
                    isUsagePage && SIDEBAR_ACTIVE_HIGHLIGHT,
                  )}
                  data-testid="usage-nav"
                >
                  <Link to="/usage" onClick={onNavClick} componentId="sidebar.usage">
                    <WalletIcon
                      className={cn(
                        "ui-icon",
                        isUsagePage
                          ? "text-[var(--sidebar-active-foreground)]"
                          : "text-muted-foreground",
                      )}
                    />
                    Usage
                  </Link>
                </Button>
              )}
            </div>

            {/* Wrapper (not the `aside`) anchors the floating Settings button:
          absolute-positioning inside the aside would place it in the native
          safe-area padding, under the home indicator. */}
            <div className="relative flex min-h-0 flex-1 flex-col">
              <nav
                ref={scrollContainerRef}
                // Keep wheel/touch scrolling without letting classic-scrollbar
                // platforms reserve a wide, permanently visible Sidebar gutter.
                // max-md:pb-14 is the floating Settings chip's clearance: the
                // chip is a non-scrolling sibling pinned bottom-right, so
                // without a gutter the last row's always-visible kebab parks
                // underneath it and can't be tapped.
                className="relative flex-1 overflow-y-auto px-2 pt-4 pb-3 [scrollbar-width:none] max-md:pb-16 [&::-webkit-scrollbar]:hidden"
              >
                <ConversationList
                  conversationsQuery={conversationsQuery}
                  scrollContainerRef={scrollContainerRef}
                  onRowClick={onNavClick}
                  searchQuery=""
                  newSessionProjectName={newSessionProjectName}
                  activeTab={activeTab}
                  onActiveTabChange={switchTab}
                  multiUser={multiUser}
                  pinnedConversationIds={pinnedConversationIds}
                  pinnedConversations={pinnedConversations}
                  onTogglePinned={togglePinnedConversation}
                  onEnterSelectionMode={enterSelectionMode}
                  selectionMode={selectionMode}
                  selectionScope={selectionScope}
                  selectedIds={selectedIds}
                  onToggleSelected={toggleSelected}
                  onDeselectAll={deselectAll}
                  onExitSelectionMode={exitSelectionMode}
                  getVisibleIdsRef={getVisibleIdsRef}
                />
              </nav>
              {/* Mobile: Settings floats over the bottom of the session list, with
          Search floating at the top of the header row — the two icons the
          drawer keeps once the collapse toggle is gone. */}
              <SidebarSettingsButton
                testId="sidebar-settings-float"
                className="absolute right-3 bottom-3 md:hidden"
              />
            </div>

            {/* Desktop server picker, pinned below the scrolling session list.
          Self-hiding: renders nothing outside the Electron shell (see
          SidebarServerPicker), so browsers keep an unchanged sidebar that ends
          with the list. */}
            <SidebarServerPicker />
          </>
        )}
      </aside>
    </>
  );
}

/**
 * Auto-loading pagination control. An IntersectionObserver fetches the next
 * page when this nears view (rooted on the scroll container, pre-fetching 200px
 * early for smoothness); the button stays clickable as an a11y /
 * no-IntersectionObserver fallback. Renders nothing once there's no more to
 * load. Shared by the global list and each project folder.
 */
function InfiniteScrollSentinel({
  hasMore,
  isFetching,
  fetchMore,
  scrollRoot,
  indent,
}: {
  hasMore: boolean;
  isFetching: boolean;
  fetchMore: () => void;
  scrollRoot: RefObject<HTMLElement | null>;
  indent?: boolean;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const sentinel = ref.current;
    if (!sentinel || !hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && !isFetching) fetchMore();
      },
      { root: scrollRoot.current, rootMargin: "200px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, isFetching, fetchMore, scrollRoot]);

  if (!hasMore) return null;
  return (
    <button
      ref={ref}
      type="button"
      disabled={isFetching}
      onClick={() => {
        if (hasMore) fetchMore();
      }}
      className={cn(
        "flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-muted-foreground text-sm hover:bg-muted disabled:pointer-events-none disabled:opacity-50",
        indent && "pl-5",
      )}
    >
      {isFetching ? (
        <>
          <Loader2Icon className="size-3 animate-spin" />
          Loading…
        </>
      ) : (
        "Load more"
      )}
    </button>
  );
}

/**
 * One project folder. Fetches its own sessions server-side (`?project=`) so it
 * shows ALL its members regardless of how far the global sidebar list has been
 * scrolled, paginated with its own infinite-scroll sentinel. Lazy: the fetch is
 * gated on `expanded`, so a collapsed folder costs nothing. The collapsed
 * `marker` is supplied by the parent (best-effort, from the globally-loaded
 * window) since a collapsed folder hasn't fetched yet.
 */
function ProjectFolder({
  name,
  projectId,
  icon,
  windowConversations,
  expanded,
  active,
  marker,
  onToggleCollapsed,
  pinnedConversationIds,
  activeOverride,
  frozenSortKeys,
  scrollRoot,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  onProjectAssigned,
  onConversationsLoaded,
}: {
  name: string;
  /** First-class project id, or null for a label-only folder. */
  projectId: string | null;
  /** Chosen emoji icon (unicode grapheme), or null/absent for the default
      folder glyph. */
  icon?: string | null;
  /** This folder's members from the globally-loaded window (may lag or lead
      the folder's own pages — e.g. a just-moved row carries its optimistic
      membership here before the folder query returns it). */
  windowConversations: Conversation[];
  expanded: boolean;
  /** Whether the new-session composer is currently scoped to this project. */
  active: boolean;
  marker: SessionState | null;
  onToggleCollapsed: () => void;
  pinnedConversationIds: string[];
  activeOverride: ActiveChatOverride | null;
  /** Pointer-inside sort-key freeze shared with the flat list (see
      ConversationList); null while the pointer is outside the list. */
  frozenSortKeys: Map<string, number> | null;
  scrollRoot: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onProjectAssigned?: (projectName: string) => void;
  /** Report this folder's own loaded (rendered) sessions to the parent. The
      folder paginates independently of the global window, so bulk-selection in
      the projects scope must resolve selected rows against these — not the
      global list — or an out-of-window member would silently drop from the
      action. */
  onConversationsLoaded?: (name: string, conversations: Conversation[]) => void;
}) {
  const query = useProjectSessions(name, expanded);
  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  const conversations = useMemo(() => {
    // Union the folder's own pages with its members from the globally-loaded
    // window, window rows winning: those carry the move overlay
    // (useMoveToProject), so a just-filed session shows here immediately
    // instead of waiting out the PATCH + folder refetch round-trips.
    const byId = new Map<string, Conversation>();
    for (const c of query.data?.pages.flatMap((page) => page.data) ?? []) byId.set(c.id, c);
    for (const c of windowConversations) byId.set(c.id, c);
    // Pinned sessions live in the global Pinned section, not their folder.
    return sortByUpdatedAtDesc(
      [...byId.values()].filter((c) => !pinnedSet.has(c.id)),
      activeOverride,
      frozenSortKeys,
    );
  }, [query.data, windowConversations, pinnedSet, activeOverride, frozenSortKeys]);

  // Publish the folder's rendered rows upward so projects-scope bulk selection
  // resolves them (the parent sources its action set from these, not the global
  // paginated window).
  useEffect(() => {
    onConversationsLoaded?.(name, conversations);
  }, [name, conversations, onConversationsLoaded]);

  // While the first page loads, show a "Loading…" footer instead of the "No
  // chats" empty state (which would otherwise flash before rows arrive).
  const loadingFirstPage = expanded && query.isLoading;

  // The whole folder (collapsed header included) is a drop target: releasing a
  // dragged session anywhere on it files the session into this project. The
  // `project:` prefix keeps the droppable id clear of conversation ids (the
  // draggable ids) and the ungroup sentinel.
  const { setNodeRef, isOver } = useDroppable({
    id: `project:${name}`,
    data: { type: "project", name },
  });

  const { actions: menuActions, dialogs: menuDialogs } = useProjectFolderMenu(
    name,
    projectId,
    icon,
  );

  return (
    <div
      ref={setNodeRef}
      className={cn(
        "rounded-[var(--radius-otto-sm)] transition-colors duration-200 ease-[var(--ease-otto)]",
        // Subtle background tint on drag-over — no border, no shadow.
        isOver && DROP_TARGET_HIGHLIGHT,
      )}
    >
      <ConversationSection
        title={name}
        icon={
          icon ? (
            <span className="text-[14px] leading-none">{icon}</span>
          ) : expanded ? (
            <FolderOpenIcon
              className={cn(
                "ui-icon",
                active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
              )}
            />
          ) : (
            <FolderIcon
              className={cn(
                "ui-icon",
                active ? "text-[var(--sidebar-active-foreground)]" : "text-muted-foreground",
              )}
            />
          )
        }
        active={active}
        marker={marker}
        conversations={conversations}
        pinnedConversationIds={pinnedConversationIds}
        // Projects default collapsed: shown only when explicitly expanded.
        collapsed={!expanded}
        onToggleCollapsed={onToggleCollapsed}
        onRowClick={onRowClick}
        onTogglePinned={onTogglePinned}
        selectionMode={selectionMode}
        selectedIds={selectedIds}
        onToggleSelected={onToggleSelected}
        onProjectAssigned={onProjectAssigned}
        emptyMessage={
          loadingFirstPage ? undefined : (
            <span className="block text-ui">
              No sessions. Start a{" "}
              <Link
                to={`/?project=${encodeURIComponent(name)}`}
                className="font-medium text-primary underline-offset-4 hover:underline"
                onClick={(e) => {
                  e.stopPropagation();
                  onRowClick(e);
                }}
              >
                new session
              </Link>
              .
            </span>
          )
        }
        indentRows
        headerAction={
          <ProjectFolderActions projectName={name} onNavigate={onRowClick} actions={menuActions} />
        }
        // Touch exposes these actions through the header's long-press menu.
        actionHoverOnly
        headerContextMenu={
          <ContextMenuContent className="min-w-40">
            <ProjectFolderMenuItems
              components={contextBundle}
              projectName={name}
              onNavigate={onRowClick}
              actions={menuActions}
            />
          </ContextMenuContent>
        }
        footer={
          loadingFirstPage ? (
            <p className="px-2 py-1 pl-5 text-muted-foreground text-sm">Loading…</p>
          ) : (
            <InfiniteScrollSentinel
              hasMore={query.hasNextPage}
              isFetching={query.isFetchingNextPage}
              fetchMore={query.fetchNextPage}
              scrollRoot={scrollRoot}
              indent
            />
          )
        }
      />
      {menuDialogs}
    </div>
  );
}

interface ConversationListProps {
  conversationsQuery: ReturnType<typeof useConversations>;
  // The scrollable ancestor, used as the infinite-scroll observer root.
  scrollContainerRef: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  searchQuery: string;
  /** Project selected on the new-session composer route, if any. */
  newSessionProjectName: string | null;
  activeTab: SidebarTab;
  onActiveTabChange: (tab: SidebarTab) => void;
  /** Multi-user server; gates the "Shared" filter option. */
  multiUser: boolean;
  pinnedConversationIds: string[];
  // The server-authoritative pinned sessions, so a pinned session that sits
  // outside the loaded pagination window still renders in the Pinned section.
  pinnedConversations: Conversation[];
  onTogglePinned: (conversationId: string) => void;
  onEnterSelectionMode: (scope: SelectionScope) => void;
  selectionMode: boolean;
  selectionScope: SelectionScope;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onDeselectAll: () => void;
  onExitSelectionMode: () => void;
  getVisibleIdsRef: RefObject<() => string[]>;
}

// Ownership drives the My-vs-Shared split and every owner-only row action.
// It is derived purely from the session's `owner` (the creator's user id),
// NOT from `permission_level` — the sidebar carries no effective-level info,
// so the server can list rows without resolving the caller's grant per
// session. A `null`/absent owner (permissions disabled — the server emits
// `owner` only when a permission store is wired) reads as owned, matching the
// prior permissive-on-null stance; otherwise the viewer owns it iff they are
// the owner. In single-user mode the owner grant is the reserved `"local"`
// id, and `viewerId` is `"local"` too (see `useViewerId`), so it matches via
// the equality branch. `viewerId` is `null` until identity resolves — treated
// as "not the owner" for shared rows so they don't briefly flash into "My
// sessions" before the id lands.
function isOwnedByViewer(conversation: Conversation, viewerId: string | null): boolean {
  const owner = conversation.owner ?? null;
  if (owner === null) return true;
  return owner === viewerId;
}

// The current viewer's user id, resolved reactively. Uses `getCurrentUserId`
// (NOT `getCurrentAuthorId`): ownership compares against the session's `owner`
// grant, which in single-user mode is the reserved `"local"` id — and
// `getCurrentAuthorId` nulls `"local"` out (it's for author labels), which
// would make the viewer's own sessions read as shared and vanish from the
// default "My sessions" tab. `getCurrentUserId` keeps `"local"` and is the
// identical real email in multi-user mode. It is synchronous (populated once
// `resolveIdentity` has run — which `main.tsx` kicks off at boot), but on a
// cold mount it can still be null for a tick, so we also await
// `resolveIdentity()` and re-render when it lands. Keeping this reactive
// (rather than a bare module read) means the My/Shared split settles correctly
// the moment identity is known, without a manual refresh.
function useViewerId(): string | null {
  const [viewerId, setViewerId] = useState<string | null>(() => getCurrentUserId());
  useEffect(() => {
    let cancelled = false;
    void resolveIdentity().then(() => {
      if (!cancelled) setViewerId(getCurrentUserId());
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return viewerId;
}

function ConversationList({
  conversationsQuery,
  scrollContainerRef,
  onRowClick,
  searchQuery,
  newSessionProjectName,
  activeTab,
  onActiveTabChange,
  multiUser,
  pinnedConversationIds,
  pinnedConversations,
  onTogglePinned,
  onEnterSelectionMode,
  selectionMode,
  selectionScope,
  selectedIds,
  onToggleSelected,
  onDeselectAll,
  onExitSelectionMode,
  getVisibleIdsRef,
}: ConversationListProps) {
  // Viewer id for the owner-based My/Shared split below.
  const viewerId = useViewerId();
  // Host metadata is shared by every row tooltip. Resolve it once at the list
  // owner so ordinary rows do not each create their own polling observer.
  const { data: hosts = [] } = useHosts({ includeSandbox: true });
  const hostsById = useMemo(
    () => new Map(hosts.map((host) => [host.host_id, host] as const)),
    [hosts],
  );
  // All loaded conversations from the single paginated list (for the flat
  // session list; pinned rows are merged in from the server pinned query).
  const allConversations = useMemo(
    () => conversationsQuery.data?.pages.flatMap((page) => page.data) ?? [],
    [conversationsQuery.data],
  );

  // Project folders ({ id, name }) for grouping sessions — first-class id
  // and/or the legacy omni_project label, unioned server-side.
  const { data: projects = [] } = useProjects();

  // id → name for the rows' project_id lookup, built once here and shared via
  // context so a row doesn't subscribe to useProjects() itself.
  const projectNamesById = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of projects) {
      if (p.id !== null) map.set(p.id, p.name);
    }
    return map;
  }, [projects]);

  // Freeze the active chat's sort key while you're inside it so an
  // updated_at bump from sending a message doesn't reorder the row
  // out from under you. Snapshot is dropped on navigate-away so the
  // chat snaps back to its real position once you've left.
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const [activeOverride, setActiveOverride] = useState<ActiveChatOverride | null>(null);
  useEffect(() => {
    setActiveOverride((prev) => computeNextActiveOverride(activeId, allConversations, prev));
  }, [activeId, allConversations]);

  // While the pointer is inside the list OR a rename edit is open, pin every
  // row's sort key so background updated_at bumps can't reorder rows under
  // the cursor / around the edit input — a row sliding into place
  // mid-interaction receives the click / right-click and the rename it
  // triggers, hitting a session the user never aimed at; a reorder during an
  // edit can move (and blur) the input, committing a half-typed title. The
  // map accumulates keys lazily inside sortByUpdatedAtDesc (a render-time ref
  // write) and is cleared once neither hold is active, when the order snaps
  // back to reality.
  const frozenKeysRef = useRef<Map<string, number>>(new Map());
  const [pointerInside, setPointerInside] = useState(false);
  const [editingIds, setEditingIds] = useState<ReadonlySet<string>>(() => new Set());
  const reportRowEditing = useCallback((id: string, editing: boolean) => {
    setEditingIds((prev) => {
      if (prev.has(id) === editing) return prev;
      const next = new Set(prev);
      if (editing) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const orderFrozen = pointerInside || editingIds.size > 0;
  const frozenKeys = orderFrozen ? frozenKeysRef.current : null;
  useEffect(() => {
    if (!orderFrozen) frozenKeysRef.current.clear();
  }, [orderFrozen]);

  // Build sections: Pinned and Archived are peeled off; the rest splits into
  // the viewer's own sessions (Chats) and ones shared with them. Archived
  // sessions render in their own group at the bottom (below "Shared with
  // me"); a pinned-then-archived session shows under Archived, not Pinned.
  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  const sections = useMemo(() => {
    // Merge the server pinned set in, so a pinned session outside the loaded
    // paginated window still renders. Dedupe by id: a pinned session is usually
    // also present in the paginated list, and merging both would render it twice.
    const allWithPinned = dedupeConversationsById([...allConversations, ...pinnedConversations]);
    const notArchived = allWithPinned.filter((c) => c.archived !== true);
    // The filter picks the slice; the Pinned / Projects / Sessions structure is
    // then built from it, so every filter reuses the same layout.
    const tabScoped =
      activeTab === "archived"
        ? allWithPinned.filter((c) => c.archived === true)
        : activeTab === "shared"
          ? notArchived.filter((c) => !isOwnedByViewer(c, viewerId))
          : activeTab === "mine"
            ? notArchived.filter((c) => isOwnedByViewer(c, viewerId))
            : notArchived;

    // Pinned takes precedence over Project: pinning a session moves it OUT of
    // its project into the flat global Pinned section (no nested pins). Ordered
    // by when they were pinned (the `omnigent.pinned` label's epoch-ms value;
    // oldest pin at the top, newest at the bottom), NOT by `updated_at`, so a
    // pinned session holds its slot when a new message bumps its `updated_at`.
    // Pins are ownership-agnostic, so the Pinned section always shows every
    // non-archived pin regardless of the My/Shared/All/Archived filter — it's
    // scoped to notArchived (not tabScoped), so an owned pin stays visible on
    // the Shared tab, a shared pin stays visible on My sessions, and the pins
    // don't vanish on the Archived tab either. (An archived session is never
    // pinned into the live sections — hence notArchived, not allWithPinned.)
    const pinned = orderByPinnedTimestamp(notArchived.filter((c) => pinnedSet.has(c.id)));
    const pinnedIdSet = new Set(pinned.map((c) => c.id));

    // The Projects section renders the same folders on every filter (scope to
    // notArchived, not tabScoped, so folders don't empty out on Shared or
    // Archived). Filing is owner-only, though — UNLIKE pins — so membership is
    // gated on ownership: a folder only ever holds the viewer's OWNED sessions.
    // Without the guard the legacy label arm would match a shared session by
    // project name alone, pulling a foreign session into the viewer's folder
    // (and out of the flat Shared list via filedIds). Each folder holds its
    // non-pinned sessions — pinning a project's last one leaves it empty.
    const filedIds = new Set<string>();
    const projectGroups: {
      id: string | null;
      name: string;
      icon?: string | null;
      conversations: Conversation[];
    }[] = projects.map(({ id, name, icon }) => {
      // Dual-read membership: a session belongs to this folder if it has
      // the first-class id OR the legacy omni_project label of this name,
      // and (filing being owner-only) the viewer owns it.
      const inProject = notArchived.filter(
        (c) =>
          isOwnedByViewer(c, viewerId) &&
          ((id !== null && c.project_id === id) || c.labels?.[PROJECT_LABEL_KEY] === name) &&
          !pinnedIdSet.has(c.id),
      );
      inProject.forEach((c) => filedIds.add(c.id));
      return {
        id,
        name,
        icon,
        conversations: sortByUpdatedAtDesc(inProject, activeOverride, frozenKeys),
      };
    });
    // NOTE: empty projects are intentionally NOT filtered out. A project comes
    // from the server project list (useProjects), so it can have zero *loaded*
    // conversations — either genuinely empty or because its chats live on an
    // unloaded page. We render it as a folder with a "No sessions" placeholder
    // rather than hiding it (matches the target sidebar layout).

    // Sessions: the remainder — not pinned, not filed.
    const sessions = sortByUpdatedAtDesc(
      tabScoped.filter((c) => !pinnedIdSet.has(c.id) && !filedIds.has(c.id)),
      activeOverride,
      frozenKeys,
    );
    return { pinned, sessions, projectGroups };
  }, [
    allConversations,
    pinnedConversations,
    pinnedSet,
    activeOverride,
    frozenKeys,
    projects,
    activeTab,
    viewerId,
  ]);

  // Scope-active flags: which section owns the current selection UI (checkboxes
  // + bulk-action bar). Only one is ever true at a time.
  const sessionsSelecting = selectionMode && selectionScope === "sessions";
  const projectsSelecting = selectionMode && selectionScope === "projects";

  // Collapsed section titles — persisted like pins so the preference
  // survives reloads. Lifted here (not per-section state) because the
  // baseline group's "Recent" title comes and goes with its siblings.
  const [collapsedSections, setCollapsedSections] = useState<string[]>(
    readCollapsedSidebarSections,
  );
  const toggleSectionCollapsed = useCallback((sectionTitle: string) => {
    setCollapsedSections((prev) => {
      const next = prev.includes(sectionTitle)
        ? prev.filter((t) => t !== sectionTitle)
        : [...prev, sectionTitle];
      writeCollapsedSidebarSections(next);
      return next;
    });
  }, []);

  // Auto-expand the Pinned section when a session is newly pinned, so a
  // freshly-pinned chat can't hide inside a collapsed group. Only reacts to
  // pins being *added* — unpinning or reordering leaves the collapsed
  // preference alone.
  const prevPinnedIds = useRef(pinnedConversationIds);
  useEffect(() => {
    const prev = new Set(prevPinnedIds.current);
    const wasPinned = pinnedConversationIds.some((id) => !prev.has(id));
    prevPinnedIds.current = pinnedConversationIds;
    if (wasPinned) {
      setCollapsedSections((prevCollapsed) => {
        if (!prevCollapsed.includes("Pinned")) return prevCollapsed;
        const next = prevCollapsed.filter((t) => t !== "Pinned");
        writeCollapsedSidebarSections(next);
        return next;
      });
    }
  }, [pinnedConversationIds]);

  // When a search query appears, auto-expand all sections so results
  // in collapsed groups are visible. The user can still manually collapse
  // sections while searching. When the search is cleared, restore the
  // persisted collapsed state.
  const prevSearchQuery = useRef(searchQuery);
  const [searchCollapsedSections, setSearchCollapsedSections] = useState<string[]>([]);
  useEffect(() => {
    const wasEmpty = !prevSearchQuery.current;
    const isNonEmpty = !!searchQuery;
    prevSearchQuery.current = searchQuery;
    if (wasEmpty && isNonEmpty) {
      setSearchCollapsedSections([]);
    }
  }, [searchQuery]);
  const effectiveCollapsedSections = searchQuery ? searchCollapsedSections : collapsedSections;
  const effectiveToggleSectionCollapsed = searchQuery
    ? (sectionTitle: string) => {
        setSearchCollapsedSections((prev) =>
          prev.includes(sectionTitle)
            ? prev.filter((t) => t !== sectionTitle)
            : [...prev, sectionTitle],
        );
      }
    : toggleSectionCollapsed;

  // Project folders default to COLLAPSED, so we track the inverse — names the
  // user has expanded — persisted across reloads. A project shows its rows only
  // while its name is in this set.
  const [expandedProjects, setExpandedProjects] = useState<string[]>(readExpandedProjectSections);
  const toggleProjectExpanded = useCallback((projectName: string) => {
    setExpandedProjects((prev) => {
      const next = prev.includes(projectName)
        ? prev.filter((n) => n !== projectName)
        : [...prev, projectName];
      writeExpandedProjectSections(next);
      return next;
    });
  }, []);
  // Expand a project (idempotent). Called right after a session is filed into
  // one, so the freshly populated folder — especially a brand-new project —
  // opens to reveal the session instead of appearing collapsed.
  const expandProject = useCallback((projectName: string) => {
    setExpandedProjects((prev) => {
      if (prev.includes(projectName)) return prev;
      const next = [...prev, projectName];
      writeExpandedProjectSections(next);
      return next;
    });
  }, []);

  // ── Drag-and-drop: file sessions into / out of projects ────────────────────
  // A session row can be dragged onto a project folder (file it there), onto the
  // "Chats" list / a fallback strip (unfile it), or onto "Pinned" (pin it, which
  // floats it out of its project). "Shared with me" is deliberately not a drop
  // target — you can't file sessions there. The kebab "Move session" menu + the
  // pin button remain the keyboard-accessible paths; DnD is a pointer
  // enhancement on top of them, so the sensors are pointer-only.
  const moveToProject = useMoveToProject();
  // The session currently being dragged (id + source project + pinned state), or
  // null. Set on drag start, cleared on end/cancel; drives the DragOverlay
  // preview and which drop zones light up (ungroup only for a filed session, pin
  // only for an unpinned one).
  const [activeDrag, setActiveDrag] = useState<{
    id: string;
    label: string;
    project: string | null;
    isPinned: boolean;
  } | null>(null);
  // Mouse: a small drag threshold so a plain click still navigates / opens the
  // kebab. Touch: a press-and-hold delay so scrolling the list isn't hijacked
  // into a drag. Keyboard users use the kebab menu instead (no KeyboardSensor).
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );
  const handleDragStart = useCallback((event: DragStartEvent) => {
    const data = event.active.data.current as
      { label?: string; project?: string | null; isPinned?: boolean } | undefined;
    setActiveDrag({
      id: String(event.active.id),
      label: data?.label ?? String(event.active.id),
      project: data?.project ?? null,
      isPinned: data?.isPinned ?? false,
    });
  }, []);
  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const dragged = activeDrag;
      setActiveDrag(null);
      if (!dragged) return;
      const target = (event.over?.data.current as SidebarDropTarget | undefined) ?? null;
      const action = resolveSidebarDrop(
        { id: dragged.id, project: dragged.project, isPinned: dragged.isPinned },
        target,
      );
      if (action.kind === "move") {
        moveToProject.mutate({ id: dragged.id, project: action.project });
        // Unpin a pinned session so it actually drops into the folder instead of
        // staying floated up in Pinned (pin outranks project membership).
        if (action.unpin) onTogglePinned(dragged.id);
        // Open the (possibly brand-new) folder so the session is visible in it.
        expandProject(action.project);
        return;
      }
      if (action.kind === "pin" || action.kind === "unpin") {
        // Toggle the pin: `pin` is only emitted for an unpinned session, `unpin`
        // only for a pinned one, so a single toggle lands the intended state.
        // Unpinning a pinned session drops it back into its project / Chats.
        onTogglePinned(dragged.id);
        return;
      }
      if (action.kind === "ungroup") {
        // Unfile silently — a first-class project persists when emptied, so
        // dragging out its last session deletes nothing. Mirrors the kebab flow.
        moveToProject.mutate({ id: dragged.id, project: "" });
        if (action.unpin) onTogglePinned(dragged.id);
      }
    },
    [activeDrag, moveToProject, expandProject, onTogglePinned],
  );

  const expandAllProjects = useCallback((allNames: string[]) => {
    setExpandedProjects(() => {
      writeExpandedProjectSections(allNames);
      return allNames;
    });
  }, []);
  const collapseAllProjects = useCallback(() => {
    setExpandedProjects(() => {
      writeExpandedProjectSections([]);
      return [];
    });
  }, []);

  // Sessions each expanded ProjectFolder has actually rendered, keyed by
  // project name. A folder paginates independently of the global window, so its
  // rows can include members the global list hasn't loaded; projects-scope
  // selection must resolve against these to avoid silently dropping an
  // out-of-window row from a bulk action. Folders report via
  // `onConversationsLoaded`; collapsed folders report `[]`.
  const [folderConversations, setFolderConversations] = useState<Map<string, Conversation[]>>(
    () => new Map(),
  );
  const handleFolderConversationsLoaded = useCallback(
    (name: string, conversations: Conversation[]) => {
      setFolderConversations((prev) => {
        const existing = prev.get(name);
        // Skip the update when the id set is unchanged, so a background refetch
        // that returns the same rows doesn't churn state (and re-render).
        if (
          existing &&
          existing.length === conversations.length &&
          existing.every((c, i) => c.id === conversations[i]?.id)
        ) {
          return prev;
        }
        const next = new Map(prev);
        next.set(name, conversations);
        return next;
      });
    },
    [],
  );

  // The projects-scope selection pool: the folders' own rendered rows (the
  // authoritative, possibly-out-of-window set) unioned with the global-derived
  // membership as a fallback for folders that haven't reported yet. Deduped by
  // id. This backs the bulk-action bar, the shift-select range, and the
  // stranding guard so all three agree on what's selectable.
  const projectSessionPool = useMemo(() => {
    const byId = new Map<string, Conversation>();
    for (const group of sections.projectGroups) {
      for (const c of group.conversations) byId.set(c.id, c);
    }
    for (const rows of folderConversations.values()) {
      for (const c of rows) byId.set(c.id, c);
    }
    return [...byId.values()];
  }, [sections.projectGroups, folderConversations]);

  // The bulk-action bar lives under the header of the section it targets, so it
  // unmounts when that section empties (e.g. every selected session
  // archived/deleted). Exit selection mode in that case so the user isn't
  // stranded without its controls. Suppressed while the list is refetching: a
  // background refetch can briefly yield an empty page, and exiting on that
  // transient would kick the user out of selection mode mid-task. The projects
  // pool unions global-derived membership with the folder queries, so a single
  // folder's transient-empty refetch can't zero it while any member is still in
  // the global window (only a genuinely empty pool exits).
  useEffect(() => {
    if (!selectionMode || conversationsQuery.isFetching) return;
    const pool =
      selectionScope === "projects" ? projectSessionPool.length : sections.sessions.length;
    if (pool === 0) onExitSelectionMode();
  }, [
    selectionMode,
    selectionScope,
    sections.sessions.length,
    projectSessionPool.length,
    conversationsQuery.isFetching,
    onExitSelectionMode,
  ]);

  // The project the currently-selected session is filed under, if any. Derived
  // as a primitive so the auto-expand effect below only fires when the
  // selection (or its project) changes — not on every background list refetch,
  // which would re-open a folder the user just collapsed.
  const activeProjectName = useMemo(() => {
    if (!activeId) return null;
    const active = allConversations.find((c) => c.id === activeId);
    return active?.labels?.[PROJECT_LABEL_KEY] ?? null;
  }, [activeId, allConversations]);
  // Auto-expand the project folder holding the selected session, so navigating
  // to a filed session reveals it instead of leaving it hidden in a collapsed
  // folder. Skipped for pinned sessions: they're already reachable from the
  // Pinned section, so forcing their project open would undo a manual collapse
  // every time the user clicks the pinned row.
  useEffect(() => {
    if (!activeId || !activeProjectName) return;
    if (pinnedSet.has(activeId)) return;
    expandProject(activeProjectName);
  }, [activeId, activeProjectName, pinnedSet, expandProject]);

  // Visible rows in render order (collapsed sections excluded) for the Cmd+↑/↓
  // session hotkey. Titles must match the <ConversationSection> props below.
  const orderedConversationIds = useMemo(() => {
    const visible = (title: string, list: readonly Conversation[]) =>
      effectiveCollapsedSections.includes(title) ? [] : list;
    // A project's chats are navigable only when the "Projects" group is
    // expanded AND that individual project folder is expanded (folders are
    // collapsed unless explicitly opened — inverse of the fixed sections).
    const projectsCollapsed = effectiveCollapsedSections.includes("Projects");
    const projectVisible = (name: string, list: readonly Conversation[]) =>
      !projectsCollapsed && expandedProjects.includes(name) ? list : [];
    // `sections` is already scoped to the active tab, so the same Pinned /
    // Projects / Sessions walk covers both tabs (Projects is empty on shared).
    return [
      ...visible("Pinned", sections.pinned),
      ...sections.projectGroups.flatMap((g) => projectVisible(g.name, g.conversations)),
      ...visible("Chats", sections.sessions),
    ].map((c) => c.id);
  }, [sections, effectiveCollapsedSections, expandedProjects]);
  // Getter for the shift-select range, built on demand (at click time). Scopes
  // to whichever section is selectable: the flat Sessions list, or the sessions
  // across expanded project folders (in render order). For projects scope the
  // range uses each folder's own reported rows — the same source the folder
  // renders — so a shift target the global window hasn't loaded still resolves.
  // Rows outside the active scope have no checkboxes, so they never enter a range.
  getVisibleIdsRef.current = () => {
    if (selectionScope === "projects") {
      if (effectiveCollapsedSections.includes("Projects")) return [];
      return sections.projectGroups.flatMap((g) => {
        if (!expandedProjects.includes(g.name)) return [];
        const rows = folderConversations.get(g.name) ?? g.conversations;
        return rows.map((c) => c.id);
      });
    }
    return effectiveCollapsedSections.includes("Chats") ? [] : sections.sessions.map((c) => c.id);
  };
  useSessionSwitchHotkey(orderedConversationIds, activeId);

  // Cmd/Ctrl+1..9/0 jumps to the first ten pinned sessions (desktop only;
  // see the hook). Empty when the Pinned section is collapsed.
  const pinnedSessionIds = useMemo(
    () => (collapsedSections.includes("Pinned") ? [] : sections.pinned.map((c) => c.id)),
    [sections.pinned, collapsedSections],
  );
  usePinnedSessionHotkeys(pinnedSessionIds, activeId);

  // Pinned membership is server-authoritative (the `omnigent.pinned` label),
  // so there's no client-side list to normalize against the loaded window —
  // the pinned query returns exactly the pinned sessions, unpinning removes the
  // label, and a deleted session drops out of the query on the server.
  const hasMorePages = conversationsQuery.hasNextPage;
  const { fetchNextPage, isFetchingNextPage } = conversationsQuery;

  if (conversationsQuery.isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-sm">Loading…</p>;
  }
  if (conversationsQuery.isError) {
    const err = conversationsQuery.error;
    return (
      <p className="px-2 py-1 text-destructive text-ui">
        Failed to load: {err instanceof Error ? err.message : String(err)}
      </p>
    );
  }
  const showShared = activeTab === "shared";
  const emptyMessage = searchQuery ? "No matching conversations" : "No sessions";

  // Archived sessions are surfaced on the Settings page, not here, so they
  // don't count toward the sidebar's empty-state threshold. Each project
  // counts itself (not just its loaded chats) so an empty project still
  // renders its "Projects" header + "No sessions" folder rather than the global
  // empty-state message. `sections` is tab-scoped, so this counts the active
  // tab only (Projects is empty on the Shared tab).
  const totalVisible =
    sections.pinned.length +
    sections.sessions.length +
    sections.projectGroups.length +
    sections.projectGroups.reduce((sum, g) => sum + g.conversations.length, 0);

  // Section structure comes from the muted micro-headers + whitespace
  // alone (Linear-style) — no icons or counts in the headers, no divider
  // rules between groups.
  return (
    <SidebarRowDataProvider projectNamesById={projectNamesById} hostsById={hostsById}>
      <DndContext
        sensors={sensors}
        collisionDetection={pointerWithin}
        // Always-measure so the transient "remove from project" zone (mounted at
        // drag start) is registered as a drop target without a stale layout cache.
        measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragCancel={() => setActiveDrag(null)}
      >
        <RowEditHoldContext.Provider value={reportRowEditing}>
          <div
            className="flex flex-col gap-4"
            data-testid="sidebar-conversation-list"
            // Freeze the sort order while the pointer is over the list so rows
            // never move under the cursor. The frozen-keys map is cleared by the
            // effect above once no hold (pointer or open rename edit) remains.
            onMouseEnter={() => setPointerInside(true)}
            onMouseLeave={() => setPointerInside(false)}
          >
            {/* Removing a filed session from its project means dropping it back
            onto the flat "Chats" list — so the Chats section itself is the
            ungroup target (wrapped below). This top strip is only a FALLBACK
            for when there are no ungrouped chats yet, so the Chats section
            isn't rendered and there'd otherwise be nowhere to drop. */}
            {!showShared && activeDrag?.project != null && sections.sessions.length === 0 && (
              <UngroupDropZone />
            )}
            {totalVisible === 0 && searchQuery ? (
              <>
                <p className="px-2 py-1 text-ui text-muted-foreground">{emptyMessage}</p>
                {/* The list is one paginated stream ordered by updated_at across
              owned + shared sessions, so the current filter can be empty on the
              loaded window while its sessions live on a later page. Keep the
              sentinel mounted so pagination continues instead of stranding the
              user on a false "empty" state. */}
                {hasMorePages && (
                  <InfiniteScrollSentinel
                    hasMore={hasMorePages}
                    isFetching={isFetchingNextPage}
                    fetchMore={fetchNextPage}
                    scrollRoot={scrollContainerRef}
                  />
                )}
              </>
            ) : (
              <>
                {sections.pinned.length > 0 && (
                  // Drop a session here to pin it — pin-precedence then floats it
                  // out of any project into this section. Active only while dragging
                  // an unpinned session; outline-only highlight.
                  <PinDropZone active={activeDrag != null && !activeDrag.isPinned}>
                    <ConversationSection
                      title="Pinned"
                      conversations={sections.pinned}
                      pinnedConversationIds={pinnedConversationIds}
                      collapsed={effectiveCollapsedSections.includes("Pinned")}
                      onToggleCollapsed={() => effectiveToggleSectionCollapsed("Pinned")}
                      onRowClick={onRowClick}
                      onTogglePinned={onTogglePinned}
                      selectionMode={false}
                      selectedIds={selectedIds}
                      onToggleSelected={onToggleSelected}
                      onProjectAssigned={expandProject}
                    />
                  </PinDropZone>
                )}
                {/* Projects: a "Projects" group header, with each project rendered as
              a collapsible folder row nested beneath it. Folders default
              collapsed; an empty folder shows "No sessions". The folder icon marks
              a project row; the group/section headers carry no icon or count.
              Always shown (even with zero projects), unaffected by the filter, so
              "New project" (create-empty) stays discoverable and folders don't
              vanish when switching to Shared or Archived. */}
                <SectionGroup
                  title="Projects"
                  collapsed={effectiveCollapsedSections.includes("Projects")}
                  onToggleCollapsed={() => effectiveToggleSectionCollapsed("Projects")}
                  afterHeader={
                    projectsSelecting ? (
                      <BulkActionBar
                        selectedIds={selectedIds}
                        allConversations={projectSessionPool}
                        onDeselectAll={onDeselectAll}
                        onExit={onExitSelectionMode}
                        onProjectAssigned={expandProject}
                      />
                    ) : undefined
                  }
                  headerAction={
                    !selectionMode ? (
                      <ProjectHeaderActions
                        projectNames={sections.projectGroups.map((group) => group.name)}
                        collapsed={effectiveCollapsedSections.includes("Projects")}
                        expandedProjects={expandedProjects}
                        hasProjectSessions={sections.projectGroups.some(
                          (group) => group.conversations.length > 0,
                        )}
                        onExpandAll={expandAllProjects}
                        onCollapseAll={collapseAllProjects}
                        onProjectCreated={expandProject}
                        onEnterSelectionMode={() => onEnterSelectionMode("projects")}
                      />
                    ) : undefined
                  }
                >
                  {sections.projectGroups.map((group) => (
                    <ProjectFolder
                      key={group.name}
                      name={group.name}
                      projectId={group.id}
                      icon={group.icon}
                      windowConversations={group.conversations}
                      expanded={expandedProjects.includes(group.name)}
                      active={newSessionProjectName === group.name}
                      // Best-effort marker from the globally-loaded window: a
                      // collapsed folder hasn't fetched its own sessions yet.
                      marker={projectMarkerState(group.conversations)}
                      onToggleCollapsed={() => toggleProjectExpanded(group.name)}
                      pinnedConversationIds={pinnedConversationIds}
                      activeOverride={activeOverride}
                      frozenSortKeys={frozenKeys}
                      scrollRoot={scrollContainerRef}
                      onRowClick={onRowClick}
                      onTogglePinned={onTogglePinned}
                      selectionMode={projectsSelecting}
                      selectedIds={selectedIds}
                      onToggleSelected={onToggleSelected}
                      onProjectAssigned={expandProject}
                      onConversationsLoaded={handleFolderConversationsLoaded}
                    />
                  ))}
                  {sections.projectGroups.length === 0 &&
                    !effectiveCollapsedSections.includes("Projects") && (
                      <p className="px-2 py-1 text-ui text-muted-foreground">No projects</p>
                    )}
                </SectionGroup>
                {/* Always rendered, even with no rows: the header carries the
                    filter menu, so hiding it on an empty slice would strand the
                    viewer with no way to pick another filter. */}
                {
                  // Drop a session here to send it to the flat "Chats" list — where
                  // unfiled, unpinned sessions live. Active while dragging a filed
                  // session (removes it from its project) or a pinned one (unpins
                  // it), since both have somewhere to land here.
                  <ChatsDropZone
                    active={
                      activeDrag != null && (activeDrag.project != null || activeDrag.isPinned)
                    }
                  >
                    <ConversationSection
                      title="Sessions"
                      conversations={sections.sessions}
                      emptyMessage={SIDEBAR_FILTER_EMPTY[activeTab]}
                      pinnedConversationIds={pinnedConversationIds}
                      collapsed={effectiveCollapsedSections.includes("Chats")}
                      onToggleCollapsed={() => effectiveToggleSectionCollapsed("Chats")}
                      onRowClick={onRowClick}
                      onTogglePinned={onTogglePinned}
                      selectionMode={sessionsSelecting}
                      selectedIds={selectedIds}
                      onToggleSelected={onToggleSelected}
                      onProjectAssigned={expandProject}
                      afterHeader={
                        sessionsSelecting ? (
                          <BulkActionBar
                            selectedIds={selectedIds}
                            allConversations={sections.sessions}
                            onDeselectAll={onDeselectAll}
                            onExit={onExitSelectionMode}
                            onProjectAssigned={expandProject}
                          />
                        ) : undefined
                      }
                      headerAction={
                        // The filter stays reachable while bulk-selecting;
                        // switching scope just exits selection. Only the
                        // "select" entry point hides, being already active.
                        !selectionMode ? (
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon-xs"
                                aria-label="Select sessions"
                                data-testid="toggle-selection-mode"
                                className="text-muted-foreground"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  onEnterSelectionMode("sessions");
                                }}
                              >
                                <ListChecksIcon className="size-3.5" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent side="bottom">Select sessions</TooltipContent>
                          </Tooltip>
                        ) : undefined
                      }
                      persistentHeaderAction={
                        <SessionFilterMenu
                          value={activeTab}
                          onChange={onActiveTabChange}
                          multiUser={multiUser}
                        />
                      }
                    />
                  </ChatsDropZone>
                }
                {/* Every filter renders this same Pinned / Projects / Sessions
              tree; `sections` is scoped to the active filter's conversations,
              and Projects is empty for Shared and Archived. */}
                {/* Archived sessions are no longer listed here — they live on the
              Settings page ("Archived chats"), reachable from the footer. */}
                {/* Infinite-scroll sentinel for the global list. Pagination extends
              the Chats list, so it hides with a collapsed Chats group — a loader
              under a collapsed group reads orphaned. */}
                {!effectiveCollapsedSections.includes("Chats") && (
                  <InfiniteScrollSentinel
                    hasMore={hasMorePages}
                    isFetching={isFetchingNextPage}
                    fetchMore={fetchNextPage}
                    scrollRoot={scrollContainerRef}
                  />
                )}
              </>
            )}
          </div>
        </RowEditHoldContext.Provider>
        {/* The dragged row's preview follows the pointer (rendered in a portal),
          a compact card showing the session's title. */}
        <DragOverlay dropAnimation={null}>
          {activeDrag ? (
            <div className="pointer-events-none max-w-[16rem] truncate rounded-md border bg-card-solid px-3 py-2 text-ui shadow-tooltip">
              {activeDrag.label}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
    </SidebarRowDataProvider>
  );
}

/** Wraps the flat "Chats" section as an ungroup drop target: a filed session
    released here is removed from its project (back to the flat list, where
    unfiled sessions live). `active` gates the droppable so it only intercepts
    drops while a filed session is being dragged — at rest it's an inert
    wrapper. Outline-only highlight on drag-over (no background fill), matching
    the project folders. */
function ChatsDropZone({ active, children }: { active: boolean; children: ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({
    id: "chats-ungroup",
    data: { type: "ungroup" },
    disabled: !active,
  });
  return (
    <div
      ref={setNodeRef}
      data-testid="sidebar-chats-drop-zone"
      className={cn(
        "rounded-[var(--radius-otto-sm)] transition-colors duration-200 ease-[var(--ease-otto)]",
        active && isOver && DROP_TARGET_HIGHLIGHT,
      )}
    >
      {children}
    </div>
  );
}

/** Wraps the "Pinned" section as a pin drop target: a session released here is
    pinned, which (via the list's pin-precedence) floats it out of any project
    into this section. `active` gates the droppable so it only intercepts drops
    while dragging an unpinned session — at rest, or for an already-pinned
    session, it's an inert wrapper. Outline-only highlight on drag-over,
    matching the project folders and {@link ChatsDropZone}. */
function PinDropZone({ active, children }: { active: boolean; children: ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({
    id: "pinned-pin",
    data: { type: "pin" },
    disabled: !active,
  });
  return (
    <div
      ref={setNodeRef}
      data-testid="sidebar-pin-drop-zone"
      className={cn(
        "rounded-[var(--radius-otto-sm)] transition-colors duration-200 ease-[var(--ease-otto)]",
        active && isOver && DROP_TARGET_HIGHLIGHT,
      )}
    >
      {children}
    </div>
  );
}

/** Fallback ungroup target: a dashed strip shown at the top of the list ONLY
    while dragging a filed session when there are no ungrouped chats (so the
    {@link ChatsDropZone}-wrapped "Chats" section isn't rendered and there'd
    otherwise be nowhere to drop). Releasing on it removes the session from its
    project. The dashed border is the strip's own placeholder identity; the
    drag-over highlight is the shared subtle background tint. */
function UngroupDropZone() {
  const { setNodeRef, isOver } = useDroppable({ id: "__ungroup__", data: { type: "ungroup" } });
  return (
    <div
      ref={setNodeRef}
      data-testid="sidebar-ungroup-drop-zone"
      className={cn(
        "flex items-center gap-1.5 rounded-md border border-dashed border-border px-2 py-1.5 text-muted-foreground text-sm transition-colors",
        isOver && cn(DROP_TARGET_HIGHLIGHT, "text-foreground"),
      )}
    >
      <FolderMinusIcon className="size-3.5 shrink-0" />
      Drop here to remove from project
    </div>
  );
}

/**
 * Aggregate the sidebar marker for a project from its conversations, using
 * the same precedence a row uses (awaiting > unseen > running). Returned as a
 * {@link SessionState} so a collapsed project header can render the exact
 * same {@link SessionStateBadge} the rows do. ``null`` = no marker.
 */
function projectMarkerState(conversations: Conversation[]): SessionState | null {
  let awaiting = 0;
  let unseen = false;
  let running = false;
  for (const c of conversations) {
    const pending = c.pending_elicitations_count ?? 0;
    if (pending > 0) {
      awaiting += pending;
    } else if (isConversationUnseen(c.id, c.updated_at, c.status)) {
      unseen = true;
    } else if (c.status === "running") {
      running = true;
    }
  }
  if (awaiting > 0) return { kind: "awaiting", count: awaiting };
  if (unseen) return { kind: "unseen" };
  if (running) return { kind: "running" };
  return null;
}

// The shared collapsible header used by every sidebar section and section
// group, so they all align and animate identically (icon · title ·
// hover-chevron · collapsed marker).
function SectionHeader({
  title,
  icon,
  marker,
  active = false,
  hasAction,
  actionHoverOnly,
  hasPersistentAction,
  actionFocusVisible,
  collapsed,
  onToggleCollapsed,
  contextMenu,
  contextMenuDisabled,
}: {
  title: string;
  icon?: ReactNode;
  marker?: SessionState | null;
  /** Whether this header represents the current page context. */
  active?: boolean;
  /** Whether the section also renders header controls overlaid at the right
      edge. Those overlays are painted whenever hover isn't available (phones,
      touch tablets/laptops at any width), so the collapsed badges reserve the
      full control column there; only at rest on hover-capable desktops do the
      badges return to the rows' badge column. */
  hasAction?: boolean;
  /** Whether the header action is absent without a fine hover pointer. */
  actionHoverOnly?: boolean;
  /** Whether an always-visible control sits at the header's right edge (the
      Sessions filter), which the collapsed badges must clear even at rest on
      hover-capable desktops. */
  hasPersistentAction?: boolean;
  /** Match badge fades to an overlay revealed by focus-visible rather than
      focus-within. Section groups use this to ignore pointer-returned focus. */
  actionFocusVisible?: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Optional context-menu content for the header button. */
  contextMenu?: ReactNode;
  /** Suppresses the header context menu while another interaction owns it. */
  contextMenuDisabled?: boolean;
}) {
  const showsMarker = collapsed && marker != null;
  // Right-edge offset of the marker when no header control is painted: it
  // matches the rows' badge slot (-mr-1 trims an icon folder's px-2 to the
  // right-1 edge; mr-1 pushes a padless header out to it). One media condition
  // governs the whole header-control matrix: overlays hide (and stop
  // hit-testing) only on md+ displays whose PRIMARY pointer is a fine,
  // hover-capable one — `hover:hover` alone is not enough, since a convertible
  // with a coarse primary pointer can still report it, and its first tap would
  // be captured by the header underneath before any hover state exists.
  // Everywhere else the controls stay painted and clickable, so with
  // `hasAction` the cluster reserves the full mr-14 control column, returning
  // to this rest offset (or clearing the always-visible Sessions filter with
  // mr-7) only under that same condition. Hover-only actions keep this rest
  // offset at every capability. On fine-pointer touchscreen laptops a screen
  // tap still hit-tests before hover applies, so the folder's menu keeps its
  // "New session" fallback at every breakpoint.
  const clusterRestMargin = showsMarker ? (icon ? "-mr-1" : "mr-1") : "mr-2";
  const clusterHoverDesktopMargin = hasPersistentAction
    ? "[@media((hover:hover)_and_(pointer:fine))]:md:mr-7"
    : showsMarker
      ? icon
        ? "[@media((hover:hover)_and_(pointer:fine))]:md:-mr-1"
        : "[@media((hover:hover)_and_(pointer:fine))]:md:mr-1"
      : "[@media((hover:hover)_and_(pointer:fine))]:md:mr-2";
  const markerFade = actionHoverOnly
    ? "[@media((hover:hover)_and_(pointer:fine))]:group-hover/section:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:group-has-[[data-state=open]]/header:opacity-0"
    : "[@media((hover:hover)_and_(pointer:fine))]:md:group-hover/section:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-0";
  // The hover-only control's focus reveal (`focus-visible:not-sr-only`) is
  // ungated by pointer type, so a coarse-pointer tablet with a keyboard can
  // land focus on it. Its focus fade must therefore fire at every pointer type
  // too — otherwise the focus-revealed kebab paints over a still-visible
  // spinner/count. The hover-driven fades above stay pointer-gated (hover only
  // exists on fine). Every other header keeps its width-gated focus fade.
  const actionFocusFade = actionFocusVisible
    ? actionHoverOnly
      ? "group-has-[[data-header-controls]_:focus-visible]/header:opacity-0"
      : "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]_:focus-visible]/header:opacity-0"
    : actionHoverOnly
      ? "group-has-[[data-header-controls]:focus-within]/header:opacity-0"
      : "[@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:opacity-0";
  const button = (
    <button
      type="button"
      aria-expanded={!collapsed}
      aria-current={active ? "page" : undefined}
      onClick={(event) => {
        // A pointer click would otherwise leave the header focus-within,
        // keeping the hover-revealed controls up at rest. Keyboard toggles
        // (detail 0) keep focus for the ring.
        if (event.detail > 0) event.currentTarget.blur();
        onToggleCollapsed();
      }}
      className={cn(
        contextMenu && "select-none [-webkit-touch-callout:none]",
        icon
          ? cn(
              SIDEBAR_ROW,
              "group flex w-full items-center border-0 text-left text-foreground transition-colors",
              SIDEBAR_HOVER_HIGHLIGHT,
              contextMenu && SIDEBAR_OPEN_MENU_HIGHLIGHT,
              active && SIDEBAR_ACTIVE_HIGHLIGHT,
            )
          : "group flex w-full items-center gap-1 border-0 pt-0 pr-0 pb-1 pl-2 text-left text-sm font-normal text-muted-foreground transition-colors hover:text-foreground",
      )}
    >
      {icon ? (
        // Headers with a leading icon (project folders) swap the folder for a
        // chevron on desktop hover/focus, so the caret takes the icon's place
        // rather than trailing the name. Mobile (no hover) keeps the folder
        // icon and shows the trailing chevron below.
        <span className="relative flex size-4 shrink-0 items-center justify-center">
          <span className="flex md:transition-opacity md:group-hover:opacity-0 md:group-focus-visible:opacity-0">
            {icon}
          </span>
          <ChevronRightIcon
            className={cn(
              "absolute size-3.5 opacity-0 transition-[transform,opacity]",
              !collapsed && "rotate-90",
              "hidden md:flex md:group-hover:opacity-100 md:group-focus-visible:opacity-100",
            )}
          />
        </span>
      ) : null}
      <span className="min-w-0 truncate">{title}</span>
      {/* Trailing chevron, rotating on expand. Headers without a leading icon
            reveal it on desktop hover/focus; icon headers show it only on mobile
            (no hover) since desktop swaps the folder for the chevron above. */}
      <ChevronRightIcon
        className={cn(
          "size-3.5 shrink-0 transition-[transform,opacity]",
          !collapsed && "rotate-90",
          icon
            ? "md:hidden"
            : "md:opacity-0 md:group-hover:opacity-100 md:group-focus-visible:opacity-100",
        )}
      />
      {/* A hidden row inside this collapsed section carries a marker — surface
            the exact same badge a row would show, pinned to the right edge. */}
      {showsMarker && (
        <span
          className={cn(
            "ml-auto flex shrink-0 items-center justify-center transition-opacity",
            hasAction && !actionHoverOnly
              ? cn("mr-14", clusterHoverDesktopMargin)
              : hasPersistentAction
                ? "mr-7"
                : clusterRestMargin,
            // Dot/spinner markers get the fixed size-6 centered box (center
            // lands 16px from the edge, matching the rows). The "awaiting"
            // pill keeps its natural width so its label isn't clipped.
            isDotMarker(marker) && "w-6",
            // Fade out so the revealed kebab takes the marker's place,
            // mirroring a row's time/marker slot.
            hasAction && cn(markerFade, actionFocusFade),
          )}
        >
          <SessionStateBadge state={marker} />
        </span>
      )}
    </button>
  );

  return (
    <h2>
      {contextMenu && !contextMenuDisabled ? (
        <ContextMenu>
          <ContextMenuTrigger asChild>{button}</ContextMenuTrigger>
          {contextMenu}
        </ContextMenu>
      ) : (
        button
      )}
    </h2>
  );
}

// Scope filter on the Sessions heading. A radio group: the options are
// mutually exclusive slices of one list.
function SessionFilterMenu({
  value,
  onChange,
  multiUser,
}: {
  value: SidebarTab;
  onChange: (value: SidebarTab) => void;
  multiUser: boolean;
}) {
  const filters = multiUser
    ? SIDEBAR_FILTERS
    : SIDEBAR_FILTERS.filter((filter) => filter.value !== "shared");
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Filter sessions"
              data-testid="session-filter"
              onClick={(event) => event.stopPropagation()}
            >
              <ListFilterIcon className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">Filter sessions</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className="min-w-44 [&_[role=menuitemradio]]:text-ui">
        <DropdownMenuLabel className="text-muted-foreground text-sm">Display</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(next) => onChange(next as SidebarTab)}
        >
          {filters.map((filter) => (
            <DropdownMenuRadioItem
              key={filter.value}
              value={filter.value}
              data-testid={`session-filter-${filter.value}`}
            >
              {filter.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function ProjectHeaderActions({
  projectNames,
  collapsed,
  expandedProjects,
  hasProjectSessions,
  onExpandAll,
  onCollapseAll,
  onProjectCreated,
  onEnterSelectionMode,
}: {
  projectNames: string[];
  collapsed: boolean;
  expandedProjects: string[];
  /** Whether any project holds sessions — gates the "Select sessions" item, so
      it isn't offered when there's nothing under any folder to select. */
  hasProjectSessions: boolean;
  onExpandAll: (projectNames: string[]) => void;
  onCollapseAll: () => void;
  onProjectCreated: (projectName: string) => void;
  onEnterSelectionMode: () => void;
}) {
  const showExpandControls = !collapsed && projectNames.length > 0;
  const allExpanded =
    projectNames.length > 0 && projectNames.every((name) => expandedProjects.includes(name));
  const anyExpanded = projectNames.some((name) => expandedProjects.includes(name));
  // The kebab only carries the expand/collapse and "Select sessions" items; with
  // neither applicable (e.g. no projects yet) it would open empty, so hide it.
  const showMenu = showExpandControls || hasProjectSessions;

  return (
    <div className="flex items-center gap-0.5">
      <NewProjectButton onCreated={onProjectCreated} />
      {showMenu && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Project list actions"
              data-testid="project-list-actions"
              className="text-muted-foreground"
              onClick={(event) => event.stopPropagation()}
            >
              <MoreHorizontalIcon className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            {/* Gated independently: each hides only when it would be a no-op, so
                a mixed set offers both. */}
            {showExpandControls && !allExpanded && (
              <DropdownMenuItem
                data-testid="expand-all-projects"
                onSelect={() => onExpandAll(projectNames)}
              >
                <Maximize2Icon className="size-3.5" />
                Expand all
              </DropdownMenuItem>
            )}
            {showExpandControls && anyExpanded && (
              <DropdownMenuItem data-testid="collapse-all-projects" onSelect={onCollapseAll}>
                <Minimize2Icon className="size-3.5" />
                Collapse all
              </DropdownMenuItem>
            )}
            {hasProjectSessions && (
              <DropdownMenuItem
                data-testid="projects-select-sessions"
                onSelect={() => onEnterSelectionMode()}
              >
                <ListChecksIcon className="size-3.5" />
                Select sessions
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

// A collapsible group that nests other sections under a single header (e.g.
// "Projects" wrapping each project folder). Reuses SectionHeader so the group
// header is visually identical to a leaf section header.
function SectionGroup({
  title,
  collapsed,
  onToggleCollapsed,
  headerAction,
  afterHeader,
  children,
}: {
  title: string;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Optional control overlaid at the group header's right edge (e.g. the
      "collapse all projects" toggle). Always shown without hover support and
      hover/focus-revealed on hover-capable desktop displays. */
  headerAction?: ReactNode;
  /** Optional content rendered directly under the header, above the children
      (and shown even when collapsed) — e.g. the bulk-selection action bar. */
  afterHeader?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="group/header relative">
        <SectionHeader
          title={title}
          hasAction={headerAction != null}
          actionFocusVisible
          collapsed={collapsed}
          onToggleCollapsed={onToggleCollapsed}
        />
        {headerAction && (
          // Always visible (and hit-testable) except on md+ displays with a
          // fine, hover-capable primary pointer — phones, touch tablets and
          // coarse-pointer convertibles have no reliable hover, and the "New
          // project" control lives here (the only way to create a project).
          // On mouse/trackpad desktops it's hover/keyboard-focus-revealed.
          // Reveal on :focus-visible (keyboard) — NOT :focus-within — so
          // clicking the button with the mouse doesn't leave it stuck visible:
          // React reuses the same node when it swaps expand↔revert, so the
          // clicked button keeps focus afterward.
          <div
            data-header-controls
            className="-translate-y-1/2 absolute top-1/2 right-1 flex items-center transition-opacity [@media((hover:hover)_and_(pointer:fine))]:md:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:md:has-[:focus-visible]:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:opacity-100"
          >
            {headerAction}
          </div>
        )}
      </div>
      {afterHeader}
      {!collapsed && <div className="flex flex-col gap-0">{children}</div>}
    </section>
  );
}

function ConversationSection({
  title,
  icon,
  marker,
  active,
  conversations,
  pinnedConversationIds,
  collapsed,
  onToggleCollapsed,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  emptyMessage,
  indentRows,
  headerAction,
  actionHoverOnly,
  headerContextMenu,
  persistentHeaderAction,
  afterHeader,
  footer,
  onProjectAssigned,
}: {
  title?: string;
  /** Optional icon rendered before the title (e.g. project folder icon). */
  icon?: ReactNode;
  /** When collapsed, the aggregate marker of hidden rows (same badge as a row). */
  marker?: SessionState | null;
  /** Whether this section header represents the current page context. */
  active?: boolean;
  conversations: Conversation[];
  pinnedConversationIds: string[];
  /** Whether this section is currently collapsed. */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  /** Placeholder shown when expanded with no rows (e.g. an empty project). */
  emptyMessage?: ReactNode;
  /** Indent the rows one extra step (used to nest a project's chats). */
  indentRows?: boolean;
  /** Optional control overlaid at the header's right edge. */
  headerAction?: ReactNode;
  /** Whether `headerAction` is absent without a fine hover pointer. */
  actionHoverOnly?: boolean;
  /** Optional context-menu content opened from the header button. */
  headerContextMenu?: ReactNode;
  /** Optional control that remains visible at the header's right edge. */
  persistentHeaderAction?: ReactNode;
  /** Optional content rendered directly under the header, above the rows (and
      shown even when collapsed) — e.g. the bulk-selection action bar. */
  afterHeader?: ReactNode;
  /** Optional content rendered after the rows inside the expanded body (e.g. a
      project folder's own infinite-scroll sentinel / loading row). */
  footer?: ReactNode;
  /** Called with the project name when a row is filed into one, so the sidebar
      can expand that (possibly brand-new) project folder. */
  onProjectAssigned?: (projectName: string) => void;
}) {
  // An untitled section is always open — there's no header to collapse it.
  const isCollapsed = title != null && collapsed;
  const protectsCollapsedBadge = isCollapsed && marker != null;
  return (
    <section className="group/section relative">
      {title && (
        // Header + its hover-revealed kebab share a `group/header` scope so the
        // kebab keys off hovering the header alone — NOT the whole section,
        // which would also reveal it when hovering a child row.
        <div className="group/header relative">
          <SectionHeader
            title={title}
            icon={icon}
            marker={marker}
            active={active}
            hasAction={headerAction != null}
            actionHoverOnly={actionHoverOnly}
            hasPersistentAction={persistentHeaderAction != null}
            collapsed={isCollapsed}
            onToggleCollapsed={onToggleCollapsed}
            contextMenu={headerContextMenu}
            contextMenuDisabled={selectionMode}
          />
          {(headerAction || persistentHeaderAction) && (
            // The pointer-events gate lives on this OUTER positioned box: CSS
            // hit-testing on a pointer-events-none child falls through to its
            // nearest interactive ancestor, so gating only the inner wrapper
            // would leave this box swallowing clicks aimed at the badges
            // beneath it. The always-visible control re-enables hit-testing
            // for itself; the inner wrapper carries the opacity reveal only.
            // The focus reveal is scoped to focus WITHIN this control cluster
            // (not the whole header): keyboard focus on the header button must
            // not paint hit-testable controls over the still-visible badges —
            // the same scoping the badge fades use, keeping fade and reveal
            // symmetric.
            <div
              data-header-controls
              className={cn(
                "-translate-y-1/2 absolute top-1/2 right-1 flex items-center gap-0.5",
                // A hover-only action reveals at every width on a fine hover
                // pointer (no `md:`), so the badge it overlays is protected from
                // its at-rest hit target on narrow hover desktops too. Every
                // other header keeps the width-gated reveal.
                protectsCollapsedBadge &&
                  (actionHoverOnly
                    ? "[@media((hover:hover)_and_(pointer:fine))]:pointer-events-none [@media((hover:hover)_and_(pointer:fine))]:group-has-[[data-header-controls]:focus-within]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:group-hover/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:group-has-[[data-state=open]]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:has-[[aria-expanded=true]]:pointer-events-auto"
                    : "[@media((hover:hover)_and_(pointer:fine))]:md:pointer-events-none [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-testid=session-filter][aria-expanded=true]]/header:pointer-events-auto [@media((hover:hover)_and_(pointer:fine))]:md:has-[[aria-expanded=true]]:pointer-events-auto"),
              )}
            >
              {headerAction && (
                // Reveal keys off focus WITHIN the controls cluster (not the
                // whole header): clicking the toggle to expand/collapse focuses
                // the toggle inside SectionHeader — not this cluster — so it no
                // longer pins the actions visible, and keyboard-focusing the
                // control itself still reveals it.
                <div
                  className={cn(
                    "flex items-center transition-opacity",
                    actionHoverOnly
                      ? "[@media((hover:hover)_and_(pointer:fine))]:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:group-has-[[data-header-controls]:focus-within]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:group-hover/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:group-has-[[data-state=open]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:has-[[aria-expanded=true]]:opacity-100"
                      : "[@media((hover:hover)_and_(pointer:fine))]:md:opacity-0 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-header-controls]:focus-within]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-hover/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-state=open]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:group-has-[[data-testid=session-filter][aria-expanded=true]]/header:opacity-100 [@media((hover:hover)_and_(pointer:fine))]:md:has-[[aria-expanded=true]]:opacity-100",
                  )}
                >
                  {headerAction}
                </div>
              )}
              {persistentHeaderAction && (
                <div className="pointer-events-auto flex items-center">
                  {persistentHeaderAction}
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {afterHeader}
      {!isCollapsed && (
        <>
          {conversations.length === 0 && emptyMessage ? (
            // Expanded but empty — a project with no loaded chats (indented, in a
            // dashed well) or a top-level list whose filter matched nothing.
            indentRows ? (
              <div
                className={cn(
                  SIDEBAR_ROW,
                  "mt-1 mr-2 ml-8 flex flex-col items-start justify-center gap-1.5 px-0 py-1 pb-2 text-left md:py-1 md:pb-2",
                )}
              >
                <p className="text-ui text-muted-foreground">{emptyMessage}</p>
              </div>
            ) : (
              <p className="px-2 py-1 text-ui text-muted-foreground">{emptyMessage}</p>
            )
          ) : (
            // Indent project chats a step under the project-folder name above.
            <ul className={cn("flex flex-col", indentRows ? "gap-0 pl-6" : "gap-0")}>
              {conversations.map((conv) => (
                <ConversationRow
                  key={conv.id}
                  conversation={conv}
                  isPinned={pinnedConversationIds.includes(conv.id)}
                  onClick={onRowClick}
                  onTogglePinned={onTogglePinned}
                  selectionMode={selectionMode}
                  isSelected={selectedIds.has(conv.id)}
                  onToggleSelected={onToggleSelected}
                  onProjectAssigned={onProjectAssigned}
                />
              ))}
            </ul>
          )}
          {footer}
        </>
      )}
    </section>
  );
}

// The minimal item-prop shape shared by the dropdown- and context-menu
// primitive families (both wrappers accept a superset). Typing the bundle
// against this — rather than `ComponentProps<typeof DropdownMenuItem>` — lets
// either family satisfy `MenuComponents` so `ConversationMenuItems` can author
// the menu body once and render it under either menu kind.
interface MenuItemProps {
  asChild?: boolean;
  children?: ReactNode;
  className?: string;
  disabled?: boolean;
  textValue?: string;
  variant?: "default" | "destructive";
  // Radix's menu `onSelect` receives a native Event in both families.
  onSelect?: (event: Event) => void;
  "data-testid"?: string;
}

interface MenuComponents {
  Item: ComponentType<MenuItemProps>;
  Separator: ComponentType<{ className?: string }>;
  Sub: ComponentType<{ children?: ReactNode }>;
  SubTrigger: ComponentType<{
    children?: ReactNode;
    className?: string;
    "data-testid"?: string;
  }>;
  SubContent: ComponentType<{ children?: ReactNode; className?: string }>;
}

// Two stable bundles, one per Radix menu family. Annotated so a future prop
// divergence surfaces here rather than at the call site.
const dropdownBundle: MenuComponents = {
  Item: DropdownMenuItem,
  Separator: DropdownMenuSeparator,
  Sub: DropdownMenuSub,
  SubTrigger: DropdownMenuSubTrigger,
  SubContent: DropdownMenuSubContent,
};

const contextBundle: MenuComponents = {
  Item: ContextMenuItem,
  Separator: ContextMenuSeparator,
  Sub: ContextMenuSub,
  SubTrigger: ContextMenuSubTrigger,
  SubContent: ContextMenuSubContent,
};

/**
 * The conversation row's action menu body — authored once and rendered under
 * both the kebab {@link DropdownMenu} and the row's right-click {@link ContextMenu}
 * via the {@link MenuComponents} bundle, so the two menus stay identical.
 *
 * Radix requires a menu's Content and its Item/Sub* descendants to come from the
 * same primitive family (roving focus / keyboard nav), so the items can't simply
 * be shared as elements — they're rendered through the injected `components` set.
 */
function ConversationMenuItems({
  components: C,
  conversation,
  isPinned,
  isArchived,
  isOwner,
  sharingOff,
  isSingleUser,
  canStop,
  canMarkUnread,
  currentProject,
  onTogglePinned,
  onMarkUnread,
  onProjectAssigned,
  moveToProject,
  stopSession,
  setShareOpen,
  setIsEditing,
  setStopOpen,
  setDeleteOpen,
  setLeaveOpen,
  setMenuOpen,
  runArchive,
}: {
  components: MenuComponents;
  conversation: Conversation;
  isPinned: boolean;
  isArchived: boolean;
  isOwner: boolean;
  // Server-wide sharing kill switch (OMNIGENT_SHARING_MODE=off): disables the
  // Share item for everyone, independent of the per-user ownership check.
  sharingOff: boolean;
  // Single-user mode: hide the Share item entirely (no other users to share
  // with), rather than disabling it like sharingOff does.
  isSingleUser: boolean;
  canStop: boolean;
  // Whether "Mark as unread" applies: any row not already showing the
  // unread dot (the active thread and running sessions included).
  canMarkUnread: boolean;
  currentProject: string | null;
  onTogglePinned: (conversationId: string) => void;
  onMarkUnread: () => void;
  onProjectAssigned?: (projectName: string) => void;
  moveToProject: ReturnType<typeof useMoveToProject>;
  stopSession: ReturnType<typeof useStopSession>;
  setShareOpen: (open: boolean) => void;
  setIsEditing: (editing: boolean) => void;
  setStopOpen: (open: boolean) => void;
  setDeleteOpen: (open: boolean) => void;
  setLeaveOpen: (open: boolean) => void;
  // Closes the controlled kebab after a project pick; a no-op for the
  // (uncontrolled) context menu, which Radix closes on select automatically.
  setMenuOpen: (open: boolean) => void;
  runArchive: () => void;
}) {
  // Mobile lacks the horizontal room for a side-opening submenu, so the
  // project picker replaces the menu body in place instead of flying out
  // to the side. `view` swaps between the main actions and that sub-view;
  // desktop always renders the native side-flyout submenu regardless.
  const isMobile = useIsMobileViewport();
  const { trackClick } = useOmnigentAnalytics();
  const [view, setView] = useState<"main" | "projects">("main");

  // The project pick / create / remove flow — shared verbatim by the desktop
  // side-flyout submenu and the mobile in-place sub-view so both behave
  // identically (same moveToProject.mutate, confirmation, and menu close).
  const handleProjectSelect = (project: string) => {
    setMenuOpen(false);
    // Moving to another project is harmless — apply it now, and expand that
    // (possibly new) project so the session is visible in it rather than
    // hidden in a collapsed folder.
    if (project !== "") {
      moveToProject.mutate({ id: conversation.id, project });
      onProjectAssigned?.(project);
      return;
    }
    // Removing just unfiles the session (project_id=""). No confirmation: a
    // first-class project persists when emptied, so removal deletes nothing —
    // the folder stays and can be deleted explicitly from its own kebab.
    moveToProject.mutate({ id: conversation.id, project: "" });
  };

  // Mobile project sub-view: replaces the entire menu body in place (the
  // "Back" row flips `view` without closing the menu or navigating). Reachable
  // only via the mobile project item below, which sits behind the same
  // `isOwner` gate.
  if (isMobile && view === "projects") {
    return (
      <>
        <C.Item
          data-testid="project-picker-back"
          className="whitespace-nowrap"
          // Keep the menu open — just flip back to the main actions.
          onSelect={(e) => {
            e.preventDefault();
            setView("main");
          }}
        >
          <ChevronLeftIcon className="size-3.5" />
          Back
        </C.Item>
        <C.Separator />
        <ProjectPickerMenu
          components={C}
          currentProject={currentProject}
          onSelect={handleProjectSelect}
        />
      </>
    );
  }

  return (
    <>
      {/* Pin/Unpin — mobile-only (md:hidden); desktop uses the
          hover-revealed quick-pin button. Archived rows omit it (archive
          outranks pin). */}
      {!isArchived && (
        <C.Item
          data-testid="pin-conversation"
          className="md:hidden"
          onSelect={() => onTogglePinned(conversation.id)}
        >
          {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
          {isPinned ? "Unpin" : "Pin"}
        </C.Item>
      )}
      {/* Single-user mode has no other users to share with — omit the item
          entirely rather than showing it disabled. */}
      {!isSingleUser &&
        (isOwner && !sharingOff ? (
          <C.Item data-testid="share-conversation" onSelect={() => setShareOpen(true)}>
            <ShareIcon className="size-3.5" />
            Share
          </C.Item>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <div>
                <C.Item data-testid="share-conversation" disabled>
                  <ShareIcon className="size-3.5" />
                  Share
                </C.Item>
              </div>
            </TooltipTrigger>
            {/* Sharing-off is server-wide, so it outranks the per-user owner
                reason when both apply. */}
            <TooltipContent side="left">
              {sharingOff
                ? "Sharing has been disabled for this Omnigent server."
                : "Only the session owner can share this session"}
            </TooltipContent>
          </Tooltip>
        ))}
      {isOwner ? (
        <C.Item
          data-testid="rename-conversation"
          onSelect={() => {
            trackClick("sidebar.conversation.rename", "button");
            setIsEditing(true);
          }}
        >
          <PencilIcon className="size-3.5" />
          Rename
        </C.Item>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <C.Item data-testid="rename-conversation" disabled>
                <PencilIcon className="size-3.5" />
                Rename
              </C.Item>
            </div>
          </TooltipTrigger>
          <TooltipContent side="left">
            Only the session owner can rename this session
          </TooltipContent>
        </Tooltip>
      )}
      {/* Mark as unread — re-lights the row's pink dot so a session can
          be flagged to revisit, including the one you're currently
          viewing. Hidden only when the row already shows the dot. */}
      {canMarkUnread && (
        <C.Item
          data-testid="mark-unread-conversation"
          onSelect={() => {
            onMarkUnread();
            setMenuOpen(false);
          }}
        >
          <MailIcon className="size-3.5" />
          Mark as unread
        </C.Item>
      )}
      {/* Projects are a My-sessions-only tool, so filing is owner-only — a
          shared session shows no project affordance. */}
      {isOwner &&
        (isMobile ? (
          // Mobile: no room for a side flyout, so this item swaps the menu
          // body to the project picker in place (see the `view === "projects"`
          // branch above). `preventDefault` keeps the menu open on select.
          <C.Item
            data-testid="move-to-project"
            className="whitespace-nowrap"
            onSelect={(e) => {
              e.preventDefault();
              setView("projects");
            }}
          >
            <FolderInputIcon className="size-3.5" />
            {/* "Add to project" until the session is filed, then "Move
                session" to switch or remove it. */}
            {currentProject ? "Move session" : "Add to project"}
          </C.Item>
        ) : (
          <C.Sub>
            <C.SubTrigger data-testid="move-to-project" className="whitespace-nowrap">
              <FolderInputIcon className="size-3.5" />
              {currentProject ? "Move session" : "Add to project"}
            </C.SubTrigger>
            <C.SubContent className="min-w-56">
              {/* A native submenu flyout — no separate popover layer, so no
                  open/dismiss race with the parent menu. */}
              <ProjectPickerMenu
                components={C}
                currentProject={currentProject}
                onSelect={handleProjectSelect}
              />
            </C.SubContent>
          </C.Sub>
        ))}
      {/* Stop / Archive / Delete are grouped at the bottom, below a
          divider: lifecycle-ending actions separated from the everyday
          ones above. */}
      <C.Separator />
      {/* Stop session — only on stoppable sessions whose runner isn't
        already known-offline (canStop). Owner-gated like Delete:
        non-owners see it disabled with an explanatory tooltip. */}
      {canStop &&
        (isOwner ? (
          <C.Item
            data-testid="stop-conversation"
            variant="destructive"
            onSelect={() => {
              // Clear any prior failure so a stale "couldn't stop"
              // message doesn't greet the next attempt. Must happen
              // here: Radix only fires the Dialog's onOpenChange for
              // Radix-initiated changes, not this programmatic open.
              stopSession.reset();
              setStopOpen(true);
            }}
          >
            <CircleStopIcon className="size-3.5" />
            Stop session
          </C.Item>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>
              <div>
                <C.Item data-testid="stop-conversation" disabled>
                  <CircleStopIcon className="size-3.5" />
                  Stop session
                </C.Item>
              </div>
            </TooltipTrigger>
            <TooltipContent side="left">
              Only the session owner can stop this session
            </TooltipContent>
          </Tooltip>
        ))}
      {isOwner ? (
        <C.Item data-testid="archive-conversation" onSelect={runArchive}>
          {isArchived ? (
            <ArchiveRestoreIcon className="size-3.5" />
          ) : (
            <ArchiveIcon className="size-3.5" />
          )}
          {isArchived ? "Unarchive" : "Archive"}
        </C.Item>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <C.Item data-testid="archive-conversation" disabled>
                {isArchived ? (
                  <ArchiveRestoreIcon className="size-3.5" />
                ) : (
                  <ArchiveIcon className="size-3.5" />
                )}
                {isArchived ? "Unarchive" : "Archive"}
              </C.Item>
            </div>
          </TooltipTrigger>
          <TooltipContent side="left">
            Only the session owner can {isArchived ? "unarchive" : "archive"} this session
          </TooltipContent>
        </Tooltip>
      )}
      {/* One destructive slot, resolved by ownership — NOT two items. The owner
          deletes the session; a shared-with viewer leaves it (gives up their own
          grant). Non-owners used to get Delete rendered disabled here, an
          always-dead row; Leave is the action that row should have offered all
          along, so it reuses the slot, the trash icon, and the destructive
          styling rather than adding a button beneath it. Single-user mode has no
          sharing, so it keeps the plain owner Delete. */}
      {isOwner || isSingleUser ? (
        <C.Item
          data-testid="delete-conversation"
          variant="destructive"
          onSelect={() => setDeleteOpen(true)}
        >
          <Trash2Icon className="size-3.5" />
          Delete
        </C.Item>
      ) : (
        <C.Item
          data-testid="leave-conversation"
          variant="destructive"
          onSelect={() => setLeaveOpen(true)}
        >
          <Trash2Icon className="size-3.5" />
          Leave session
        </C.Item>
      )}
    </>
  );
}

function SessionTooltipContent({
  conversation,
  hostsById,
}: {
  conversation: Conversation;
  hostsById: ReadonlyMap<string, Host>;
}) {
  const host = conversation.host_id ? hostsById.get(conversation.host_id) : undefined;
  const locationLabel = !conversation.host_id
    ? "Local machine"
    : host?.sandbox_provider
      ? sandboxOptionLabel(host.sandbox_provider)
      : (host?.name ?? conversation.host_id);

  return (
    <TooltipContent
      side="right"
      align="start"
      sideOffset={8}
      data-testid="session-tooltip-content"
      // Mirror PinnedProjectFlyoutContent's compact HoverCard look: title,
      // then muted, small-icon metadata lines.
      className="w-64 max-w-[calc(100vw-2rem)] flex-col items-stretch rounded-lg bg-popover p-2.5 text-popover-foreground whitespace-normal shadow-menu ring-1 ring-foreground/10"
    >
      <p className="sidebar-compact-text line-clamp-3 font-medium">
        {conversation.title ?? conversation.id}
        <span className="font-normal text-muted-foreground">
          {" · "}
          {relativeTime(conversation.updated_at * 1000)}
        </span>
      </p>
      <p
        data-testid="session-tooltip-location"
        className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
      >
        <LaptopIcon aria-hidden className="size-3.5 shrink-0" />
        <span className="truncate">{locationLabel}</span>
      </p>
      {conversation.git_branch && (
        <p
          data-testid="session-tooltip-branch"
          className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        >
          <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{conversation.git_branch}</span>
        </p>
      )}
    </TooltipContent>
  );
}

// Max gap between the first click and the dblclick of one double-click.
// Browsers pair clicks within ~500ms; the margin absorbs event-loop delay.
const DOUBLE_CLICK_PAIR_WINDOW_MS = 750;

function ConversationRow({
  conversation,
  isPinned,
  onClick,
  onTogglePinned,
  selectionMode,
  isSelected,
  onToggleSelected,
  onProjectAssigned,
}: {
  conversation: Conversation;
  isPinned: boolean;
  onClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  isSelected: boolean;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onProjectAssigned?: (projectName: string) => void;
}) {
  const hostsById = useContext(HostsByIdContext);
  // `useParams` reads from the active matched route. On `/`, the param is
  // undefined; on `/c/:conversationId`, it carries the active id.
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  // The sidebar lists only top-level sessions; child (sub-agent) rows are
  // omitted. When the user clicks a sub-agent in the Agents rail the active
  // id becomes the child's, which matches no row here — so highlighting on
  // the raw id alone would leave the owning session unhighlighted. Resolve
  // the active conversation's top-level root and highlight against that, so
  // the parent row stays selected while viewing any of its descendants.
  // While the resolution loads (`null`), fall back to the raw id for that
  // render — a top-level session resolves to itself, so the common case is
  // unaffected.
  const activeRootId = useActiveRootSessionId(activeId ?? null);
  const isActive = (activeRootId ?? activeId) === conversation.id;
  const navigate = useNavigate();
  // Mobile has no real hover, so a tap that navigates would also trip the
  // project flyout's HoverCard and leave it lingering over the chat. Gate the
  // flyout off below the `md` breakpoint (see `projectFlyoutName`).
  const isMobile = useIsMobileViewport();
  // When this row becomes the active conversation (e.g. a freshly created
  // session navigated to via `/c/:id`), scroll it toward the center of the
  // sidebar so it's comfortably in view rather than pinned to an edge.
  const rowRef = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (!isActive) return;
    rowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [isActive]);
  const rename = useRenameConversation();
  const del = useStopAndDeleteConversation();
  const archive = useArchiveConversation();
  const leave = useLeaveSession();
  const moveToProject = useMoveToProject();
  // The kebab's user-facing "Stop session" action. Archiving does NOT go
  // through here — the server stops the session itself once the archived
  // flag commits, so a hidden session never keeps a runner alive.
  const stopSession = useStopSession();
  const isArchived = conversation.archived === true;
  const [isEditing, setIsEditing] = useState(false);
  // Hold the list's sort order while this row's rename input is open — the
  // pointer usually drifts out of the sidebar during typing, and a reorder
  // then would shuffle rows around (or move + blur) the input. Cleanup covers
  // commit, cancel, and unmount alike. Layout effect (not passive): rename
  // can start with the pointer already outside the list (context menu is a
  // portal), and a passive effect would leave a post-paint frame where churn
  // could reorder — and blur — the just-mounted input before the hold lands.
  const reportRowEditing = useContext(RowEditHoldContext);
  useLayoutEffect(() => {
    if (!isEditing) return;
    reportRowEditing(conversation.id, true);
    return () => reportRowEditing(conversation.id, false);
  }, [isEditing, conversation.id, reportRowEditing]);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  // The kebab menu is controlled so the project submenu can close the whole
  // menu after a pick (a plain click inside the submenu wouldn't otherwise).
  const [menuOpen, setMenuOpen] = useState(false);
  // Opt-in "delete local branch" checkbox (worktree sessions only).
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const gitBranch = conversation.git_branch ?? null;
  // Every row action gates on ownership alone — the sidebar carries no
  // effective-permission level, so rename/share/move/drag are owner-only and
  // non-owners get a read-only row. (Finer-grained edit/manage affordances
  // live on the open-session view, which fetches the caller's real level.)
  // Also the id Leave revokes: leaving is a self-revoke, so it needs the
  // viewer's own id — resolved by the time a non-owned row renders, since
  // `isOwner` below is derived from it.
  const viewerId = useViewerId();
  const isOwner = isOwnedByViewer(conversation, viewerId);
  // Server-wide sharing kill switch (OMNIGENT_SHARING_MODE=off) reported by
  // /v1/info — disables the row's Share item even for managers. Fail open
  // (share enabled) while the capability probe is still loading.
  const serverInfo = useServerInfo();
  const sharingOff = serverInfo !== "loading" && serverInfo.sharing_mode === "off";
  // Single-user mode has no other users to share with, so the Share item is
  // hidden entirely (not just disabled) — mirrors the header Share button.
  const isSingleUser = isSingleUserMode(serverInfo);
  // Gates the kebab's "Stop session" item. `false` = runner known-offline
  // (already stopped — hide the destructive control); `undefined` = not yet
  // observed, don't block. Non-sticky Stop: no "Resume" affordance — the
  // next message relaunches the runner on a live host.
  const runnerOnline = useSessionRunnerOnline(conversation.id);
  const canStop =
    isSessionStoppable({
      labels: conversation.labels,
      hostId: conversation.host_id,
      runnerId: conversation.runner_id,
    }) && runnerOnline !== false;

  // The session's current project NAME, or null when unfiled — drives the
  // kebab submenu label ("Add to project" vs "Move session") and the pinned
  // flyout. Dual-read: prefer the first-class membership (project_id → name via
  // the list-level map from context — no per-row query), falling back to the
  // legacy omni_project label.
  const projectNamesById = useContext(ProjectNamesContext);
  const firstClassProjectName =
    conversation.project_id != null ? projectNamesById.get(conversation.project_id) : undefined;
  const currentProject = firstClassProjectName ?? conversation.labels?.[PROJECT_LABEL_KEY] ?? null;
  // Pinned sessions are lifted OUT of their project folder into the flat
  // "Pinned" section, so the row no longer shows which project it belongs to.
  // For those rows only, surface the project in a hover flyout. Non-pinned
  // rows already sit inside their project folder, so they don't need it.
  // Disabled on mobile: there's no hover, so a tap would open the HoverCard
  // and leave it overlaying the chat after navigation. Forcing null there
  // routes the row through the plain ContextMenu/link path and restores the
  // native `title` tooltip.
  const projectFlyoutName = !isMobile && isPinned ? currentProject : null;

  // The title the user just committed. The rename's cache write reaches this
  // row as a prop from the list above, which re-renders a tick after the row's
  // own `setIsEditing(false)` — until then the row would repaint the old name.
  const [pendingTitle, setPendingTitle] = useState<string | null>(null);
  useEffect(() => {
    if (pendingTitle === null) return;
    // Cleared once the prop carries the committed name, or once the PATCH
    // settles — the hook overlays the server's title (or rolls back on
    // failure) before flipping status, so the prop is authoritative by then.
    if (conversation.title === pendingTitle || rename.isSuccess || rename.isError) {
      setPendingTitle(null);
    }
  }, [conversation.title, pendingTitle, rename.isSuccess, rename.isError]);

  const label = pendingTitle ?? conversationDisplayLabel(conversation);
  // Subscribed so the just-recorded optimistic label flips the row
  // immediately instead of at the next conversations poll.
  const optimisticTitle = useOptimisticTitle(conversation.id);
  const isProvisionalLabel =
    pendingTitle === null && conversation.title == null && optimisticTitle !== undefined;
  const hasDraft = useHasSessionDraft(conversation.id);
  // Recompute unseen state the moment the last-seen map changes (e.g. the
  // user picks "Mark as unread" on this row) rather than waiting for the
  // next conversations poll.
  useUnseenTick();
  // The dot shows when the conversation is content-unseen AND either the
  // row isn't the one you're viewing OR you explicitly marked it unread.
  // `isConversationUnseen` still gates on status, so a *running* turn never
  // shows the dot — marking a working session unread is recorded but stays
  // invisible until the turn finishes (then the dot lights like any unseen
  // row). The explicit override only lifts the active-row suppression, so
  // flagging the thread you're currently viewing surfaces the dot at once.
  const hasUnseenMessages =
    isConversationUnseen(conversation.id, conversation.updated_at, conversation.status) &&
    (!isActive || isExplicitlyUnread(conversation.id));
  // "Mark as unread" is offered on any row not already showing the dot.
  const canMarkUnread = !hasUnseenMessages;
  // Badge precedence: a pending approval ("Needs response") outranks the
  // unread dot — a session that's both unread and awaiting input should
  // surface the actionable approval tag. The row still renders bold (the
  // unread signal) via `hasUnseenMessages` below.
  const derivedState = getSessionState(conversation);
  // The bound session's launch/relaunch window: a send is in flight (local
  // status "streaming") or the runner is auto-creating the PTY
  // (`terminalPending`), but the server hasn't confirmed `running` yet — a
  // cold boot, or a send waking a disconnected runner. Without this the row
  // shows nothing while the session is visibly "Starting up…" in the chat.
  // Only the bound (open) conversation has this store state; other rows read
  // false, and the server-derived states above win once they land.
  const isStartingUp = useChatStore(
    (s) => s.conversationId === conversation.id && (s.status === "streaming" || s.terminalPending),
  );
  const sessionState =
    derivedState?.kind === "awaiting"
      ? derivedState
      : hasUnseenMessages
        ? { kind: "unseen" as const }
        : (derivedState ?? (isStartingUp ? { kind: "starting" as const } : null));
  // Drafts share the row's trailing indicator slot, but the active session's
  // composer already makes its draft visible. Live session state wins while
  // present; otherwise only an inactive row needs the draft marker.
  const showDraftIndicator = hasDraft && !isActive;
  const hasTrailingIndicator = sessionState !== null || showDraftIndicator;

  // Drag-and-drop: a row is grabbable when the viewer owns it (re-filing is
  // owner-only, like the Move-to-project kebab item), outside selection /
  // archive / rename modes. Dragging it onto a project folder files it there;
  // onto "Chats" unfiles it; onto "Pinned" pins it. The list-level <DndContext>
  // routes the drop; the row only advertises itself and its source project +
  // pinned state via the draggable `data`.
  const {
    listeners: dragListeners,
    setNodeRef: setDragNodeRef,
    isDragging,
  } = useDraggable({
    id: conversation.id,
    data: { type: "session", label, project: currentProject, isPinned },
    disabled: !isOwner || selectionMode || isArchived || isEditing,
  });
  // A drag ends with a synthetic click on the row's <Link> (mousedown + mouseup
  // on the same anchor still fires a click); swallow that one click so a drag
  // doesn't also navigate into the session. Flagged when a drag finishes,
  // cleared on the next tick (after the click that follows pointer-up).
  const justDraggedRef = useRef(false);
  const wasDraggingRef = useRef(false);
  useEffect(() => {
    const was = wasDraggingRef.current;
    wasDraggingRef.current = isDragging;
    if (!was || isDragging) return undefined;
    justDraggedRef.current = true;
    const timer = setTimeout(() => {
      justDraggedRef.current = false;
    }, 0);
    return () => clearTimeout(timer);
  }, [isDragging]);
  // Merge the drag node ref with the row ref used for scroll-into-view.
  const setRowRef = useCallback(
    (node: HTMLLIElement | null) => {
      rowRef.current = node;
      setDragNodeRef(node);
    },
    [setDragNodeRef],
  );
  // Timestamps of the last two clicks this row received, for the dblclick
  // rename guard: the list can reorder between the two clicks of a
  // double-click (an updated_at bump slides another row under the cursor),
  // and only a row that saw both clicks may enter rename.
  const recentClickTimesRef = useRef<number[]>([]);

  if (isEditing) {
    return (
      <li>
        <ConversationEditRow
          // Prefer the just-committed name so a rename reopened before the
          // prop catches up starts from what the row shows.
          initialTitle={pendingTitle ?? conversation.title ?? ""}
          onCommit={(title) => {
            // Bail on no-op edits so we don't fire an unnecessary PATCH.
            const trimmed = title.trim();
            if (trimmed && trimmed !== (pendingTitle ?? conversation.title ?? "")) {
              // Set with the same event as `setIsEditing` so both land in one
              // render: the row swaps the input for the new name directly.
              setPendingTitle(trimmed);
              rename.mutate({ id: conversation.id, title: trimmed });
            }
            setIsEditing(false);
          }}
          onCancel={() => setIsEditing(false)}
        />
      </li>
    );
  }

  function confirmDelete() {
    // Fire-and-forget: close the dialog and drop the row immediately so the
    // user isn't blocked on the (potentially slow) DELETE — server-side
    // teardown can take seconds. The mutation removes the row from the
    // cached lists optimistically, which unmounts this component, so
    // anything that must happen on delete either runs here or lives in the
    // hook (a mutate-level callback would never fire).
    setDeleteOpen(false);
    setDeleteBranch(false);
    // Viewing the session being deleted? Leave now, so the chat surface
    // doesn't sit on an id that's about to 404.
    if (isActive) navigate("/", { replace: true });
    del.mutate({ id: conversation.id, deleteBranch: gitBranch !== null && deleteBranch });
  }

  function runArchive() {
    const nextArchived = !isArchived;
    // The archive PATCH sends only the flag: the server stops the session (and
    // tears down a host-spawned runner) in the background once it's committed.
    // A client stop too would race that one against the same runner, and the
    // loser gets a 503 from the already-killed pane.
    //
    // The row leaves the sidebar optimistically (useArchiveConversation flips
    // the cached `archived` flag in onMutate; the list filters archived rows
    // out client-side), so this component unmounts on the next frame. Leaving
    // the archived session's chat surface therefore has to happen HERE,
    // synchronously — not in an onSuccess callback that fires a round-trip
    // later with a stale `isActive`, which used to jump the user off whatever
    // session they'd switched to meanwhile. Mirrors confirmDelete.
    if (nextArchived && isActive) navigate("/", { replace: true });
    archive.mutate({ id: conversation.id, archived: nextArchived });
    // Point the user at where the session went — fire NOW, not in a mutate
    // onSuccess: the optimistic overlay unmounts this row on the next frame,
    // and per-call mutate callbacks don't fire once their observer unmounts.
    // A failed archive reconciles the row back with its own error toast.
    if (nextArchived) showArchivedToast();
  }

  function confirmLeave() {
    // Leave is a self-revoke, so it needs the viewer's own id. The menu item is
    // gated on the row NOT being owned by the viewer, which is only decidable
    // once the id has resolved — so this is non-null wherever it's reachable.
    if (viewerId === null) return;
    // Close immediately — the row drops out of the list on success, so there's
    // nothing left to show progress against. A failure surfaces as a toast
    // (the row is still there to retry from).
    setLeaveOpen(false);
    leave.mutate(
      { id: conversation.id, viewerId },
      {
        onSuccess: () => {
          // The session 404s for this user now, so don't leave them staring at
          // its chat surface. Mirrors delete/archive's post-mutation navigate.
          if (isActive) navigate("/", { replace: true });
        },
        onError: (err) => {
          const detail = err instanceof Error && err.message ? `: ${err.message}` : "";
          showToast(`Couldn't leave the session${detail}`);
        },
      },
    );
  }

  // Shared by the kebab dropdown and the right-click context menu so the two
  // menus render identical items. `setMenuOpen` is supplied per-call (the
  // controlled kebab passes the real setter; the uncontrolled context menu a
  // no-op — Radix closes it on select).
  const menuItemProps = {
    conversation,
    isPinned,
    isArchived,
    isOwner,
    sharingOff,
    isSingleUser,
    canStop,
    canMarkUnread,
    currentProject,
    onTogglePinned,
    onMarkUnread: () => markConversationUnread(conversation.id, conversation.updated_at),
    onProjectAssigned,
    moveToProject,
    stopSession,
    setShareOpen,
    setIsEditing,
    setStopOpen,
    setDeleteOpen,
    setLeaveOpen,
    runArchive,
  };

  // The clickable row surface. Extracted so it can be rendered bare (selection
  // mode) or wrapped in the right-click ContextMenuTrigger below.
  const rowLink = (
    <Link
      to={selectionMode ? "#" : `/c/${conversation.id}`}
      componentId="sidebar.conversation_switcher"
      className={cn(
        SIDEBAR_ROW,
        "relative flex flex-col justify-center text-left text-foreground transition-colors",
        SIDEBAR_HOVER_HIGHLIGHT,
        // Full width (not 100%+1rem) so the highlight stays inset from the
        // right edge, aligning with the project/folder rows above.
        "w-full",
        // Mobile drops the pin + kebab (see the trailing controls below), so it
        // reserves only what the badge needs — the same width desktop uses at
        // rest, before hover reveals the controls.
        !selectionMode &&
          (sessionState?.kind === "awaiting" ? "pr-29" : hasTrailingIndicator ? "pr-8" : "pr-2"),
        // The narrowed reserve must track exactly when the trailing controls
        // appear and the state marker fades — both keyed on `:focus-visible`.
        // `focus-within` also fires for a plain click, which shrank the reserve
        // on the selected row while the marker stayed put, sliding the title
        // under it.
        !selectionMode && "md:group-hover:pr-14 md:group-has-[:focus-visible]:pr-14",
        !selectionMode && menuOpen && "md:pr-14",
        selectionMode && "pr-2 pl-8",
        !selectionMode && isActive && SIDEBAR_ACTIVE_HIGHLIGHT,
        selectionMode && isSelected && SIDEBAR_ACTIVE_HIGHLIGHT,
      )}
      onClick={(e) => {
        recentClickTimesRef.current = [...recentClickTimesRef.current.slice(-1), performance.now()];
        // Swallow the click that trails a drag so it doesn't navigate.
        if (justDraggedRef.current) {
          e.preventDefault();
          return;
        }
        if (selectionMode) {
          e.preventDefault();
          e.stopPropagation();
          onToggleSelected(conversation.id, e.shiftKey);
          return;
        }
        onClick(e);
      }}
      onDoubleClick={(e) => {
        if (selectionMode) return;
        if (!isOwner) return;
        e.preventDefault();
        // The dblclick's own second click was already recorded above, so
        // exactly ONE recent click means the first click landed on a different
        // row — the list reordered mid-double-click and renaming here would
        // hit the wrong session. Zero recent clicks (synthetic dblclick with
        // no click events, e.g. in tests) stays allowed.
        const now = performance.now();
        const recent = recentClickTimesRef.current.filter(
          (t) => now - t <= DOUBLE_CLICK_PAIR_WINDOW_MS,
        );
        if (recent.length === 1) return;
        setIsEditing(true);
      }}
      title={isMobile ? (conversation.title ?? conversation.id) : undefined}
    >
      {/* Row 1: the session name. Working, needs-approval, unseen, and draft
          markers render in the shared trailing indicator slot below. */}
      <div className="flex w-full items-center gap-1.5">
        <span
          className={cn(
            "relative min-w-0 truncate",
            // The optimistic first-prompt label is a placeholder until the
            // server's title lands — dim it so it doesn't read as final.
            isProvisionalLabel && "italic text-muted-foreground",
          )}
        >
          {label}
          {hasUnseenMessages && <span className="sr-only"> (unread)</span>}
        </span>
      </div>
    </Link>
  );

  return (
    // Drag props on the <li> so the whole row is grabbable; `isDragging` dims
    // it. `setRowRef` merges the drag node ref with the scroll-into-view ref.
    <li
      ref={setRowRef}
      {...dragListeners}
      className={cn("group relative", isDragging && "opacity-40")}
    >
      {/* Right-click anywhere on the row opens the same actions as the kebab.
          Suppressed in selection mode (bulk-select owns the row), where the
          bare link is rendered instead. ContextMenuTrigger preventDefaults the
          native contextmenu event, so right-click never navigates; asChild
          merges its handler onto the Link, preserving left-click / double-click.
          Pinned, project-owned rows nest a HoverCardTrigger around the Link so
          hovering surfaces the project flyout — the trigger sits innermost so
          both the context menu and the hover card keep their handlers/refs on
          the Link. */}
      {selectionMode ? (
        projectFlyoutName ? (
          <HoverCard openDelay={150} closeDelay={0}>
            <HoverCardTrigger asChild>{rowLink}</HoverCardTrigger>
            <PinnedProjectFlyoutContent
              title={conversation.title ?? conversation.id}
              projectName={projectFlyoutName}
              gitBranch={gitBranch}
            />
          </HoverCard>
        ) : isMobile ? (
          rowLink
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>{rowLink}</TooltipTrigger>
            <SessionTooltipContent conversation={conversation} hostsById={hostsById} />
          </Tooltip>
        )
      ) : projectFlyoutName ? (
        <HoverCard openDelay={150} closeDelay={0}>
          <ContextMenu>
            <ContextMenuTrigger asChild>
              <HoverCardTrigger asChild>{rowLink}</HoverCardTrigger>
            </ContextMenuTrigger>
            <ContextMenuContent className="min-w-44">
              <ConversationMenuItems
                components={contextBundle}
                setMenuOpen={() => {}}
                {...menuItemProps}
              />
            </ContextMenuContent>
          </ContextMenu>
          <PinnedProjectFlyoutContent
            title={conversation.title ?? conversation.id}
            projectName={projectFlyoutName}
            gitBranch={gitBranch}
          />
        </HoverCard>
      ) : isMobile ? (
        <ContextMenu>
          <ContextMenuTrigger asChild>{rowLink}</ContextMenuTrigger>
          <ContextMenuContent className="min-w-44">
            <ConversationMenuItems
              components={contextBundle}
              setMenuOpen={() => {}}
              {...menuItemProps}
            />
          </ContextMenuContent>
        </ContextMenu>
      ) : (
        <Tooltip>
          <ContextMenu>
            <ContextMenuTrigger asChild>
              <div className="w-full">
                <TooltipTrigger asChild>{rowLink}</TooltipTrigger>
              </div>
            </ContextMenuTrigger>
            <ContextMenuContent className="min-w-44">
              <ConversationMenuItems
                components={contextBundle}
                setMenuOpen={() => {}}
                {...menuItemProps}
              />
            </ContextMenuContent>
          </ContextMenu>
          <SessionTooltipContent conversation={conversation} hostsById={hostsById} />
        </Tooltip>
      )}
      {selectionMode ? (
        <span className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2 flex items-center">
          {isSelected ? (
            <SquareCheckIcon className="size-4 text-primary" />
          ) : (
            <SquareIcon className="size-4 text-muted-foreground" />
          )}
        </span>
      ) : hasTrailingIndicator ? (
        <span
          className={cn(
            SESSION_STATE_SLOT_CLASS,
            // The wide "awaiting" pill keeps its natural width; every other
            // marker (running/starting/unseen dot, or the draft pencil) sits in
            // the fixed centered box so it lines up under the kebab.
            isDotMarker(sessionState) && SESSION_STATE_DOT_SLOT_CLASS,
          )}
        >
          {sessionState !== null ? (
            <SessionStateBadge state={sessionState} />
          ) : (
            <span
              role="img"
              aria-label="Draft"
              data-testid="conversation-draft-indicator"
              className="inline-flex h-5 shrink-0 items-center justify-center text-muted-foreground"
            >
              <MessageCircleDashedIcon aria-hidden className="size-3.5" />
            </span>
          )}
        </span>
      ) : null}
      {/* Trailing controls (pin + kebab) share one absolutely-positioned flex
          row, so their spacing is defined once (gap-0.5) and stays aligned
          with the project-folder header actions, which use the same pattern.
          The kebab is the rightmost child (pinned to right-1); the pin sits a
          gap to its left. Hidden entirely while selecting (bulk mode owns the
          row controls). */}
      {!selectionMode && (
        <div className="-translate-y-1/2 absolute top-1/2 right-1 flex items-center gap-0.5">
          {/* Archived rows omit the pin entirely: pinning is meaningless there
              (archive outranks pin), so there's no pin action even on hover. */}
          {!isArchived && (
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={isPinned ? "Unpin conversation" : "Pin conversation"}
              data-testid="quick-pin-conversation"
              className={cn(
                // Desktop-only quick affordance: hidden on mobile (the kebab's
                // Pin item below covers that), hover/focus-revealed from `md`
                // up. Pinned rows no longer keep a persistent pin marker, since
                // the "Pinned" section header (and pinned-first ordering inside
                // a project) already conveys the pinned state. Revealed glyph:
                // unpin if pinned, pin otherwise.
                //
                // `md:inline-flex` (not `md:block`): the Button base is
                // `inline-flex` and relies on it for `items-center
                // justify-center` to center the icon. `md:block` would override
                // that display and collapse the centering, leaving the glyph
                // pinned to the top-left of the button — so keep the flex
                // display when revealing it.
                "text-muted-foreground transition-opacity",
                "hidden md:inline-flex",
                "md:opacity-0 md:group-hover:opacity-100",
                "md:group-has-[:focus-visible]:opacity-100 md:group-has-[[aria-expanded=true]]:opacity-100",
              )}
              onClick={(e) => {
                // Keep the toggle click off the surrounding Link (no navigation).
                e.preventDefault();
                e.stopPropagation();
                onTogglePinned(conversation.id);
              }}
            >
              {isPinned ? (
                <PinOffIcon className="size-3.5" data-icon-size="14" />
              ) : (
                <PinIcon className="size-3.5" data-icon-size="14" />
              )}
            </Button>
          )}
          <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label="Conversation actions"
                data-testid="conversation-actions"
                // Desktop-only: the chat page's own header menu covers these
                // per-session actions on mobile, so the row kebab is dropped
                // there. From `md` up it stays hidden until hover / keyboard
                // focus, with `aria-expanded` keeping it surfaced while the menu
                // is open so the trigger doesn't vanish under the cursor.
                className={cn(
                  "text-muted-foreground transition-opacity",
                  "hidden md:inline-flex",
                  "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
                  "md:aria-expanded:opacity-100",
                )}
                onClick={(e) => {
                  // Keep the trigger click from bubbling into the Link.
                  e.preventDefault();
                  e.stopPropagation();
                }}
              >
                <MoreHorizontalIcon className="size-3.5" data-icon-size="14" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              <ConversationMenuItems
                components={dropdownBundle}
                setMenuOpen={setMenuOpen}
                {...menuItemProps}
              />
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
      <PermissionsModal sessionId={conversation.id} open={shareOpen} onOpenChange={setShareOpen} />
      <Dialog
        open={deleteOpen}
        onOpenChange={(open) => {
          setDeleteOpen(open);
          // Reset the checkbox on close so it doesn't carry over.
          if (!open) setDeleteBranch(false);
        }}
      >
        <DialogContent
          // Don't trigger the surrounding Link when the modal opens
          // — the dialog content is a portal, but defensively belt-
          // and-braces the click path.
          onClick={(e) => e.stopPropagation()}
        >
          <DialogHeader>
            <DialogTitle>Delete conversation?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> and all of its history will be
              removed. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {gitBranch !== null && (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <p className="text-sm text-muted-foreground">
                Optionally clean up the git worktree. These actions are{" "}
                <span className="font-semibold text-destructive">irreversible</span>.
              </p>
              <label className="flex cursor-pointer items-start gap-2 text-ui">
                <input
                  type="checkbox"
                  data-testid="delete-branch-checkbox"
                  checked={deleteBranch}
                  onChange={(e) => setDeleteBranch(e.target.checked)}
                  className="mt-0.5 size-4 shrink-0 accent-destructive"
                />
                <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0">
                  Delete local branch{" "}
                  <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">
                    {gitBranch}
                  </code>
                </span>
              </label>
            </div>
          )}
          {/* Drop the default footer divider + muted bar so the actions
              blend into the dialog body (same background). */}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={del.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={confirmDelete}
              disabled={del.isPending}
              componentId="sidebar.conversation.delete"
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={leaveOpen} onOpenChange={setLeaveOpen}>
        <DialogContent
          // Keep dialog clicks off the surrounding Link (same defensive
          // handling as the delete dialog above).
          onClick={(e) => e.stopPropagation()}
        >
          <DialogHeader>
            <DialogTitle>Leave session?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> will be removed from your
              sidebar. Nothing is deleted — the session and its history stay with its owner, who can
              share it with you again.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setLeaveOpen(false)}
              disabled={leave.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              data-testid="confirm-leave-conversation"
              onClick={confirmLeave}
              disabled={leave.isPending}
              componentId="sidebar.conversation.leave"
            >
              Leave
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {/* The stale-error reset lives on the kebab item's onSelect (the only
          open path) — onOpenChange only fires for Radix-initiated closes. */}
      <Dialog open={stopOpen} onOpenChange={setStopOpen}>
        <DialogContent
          // Keep dialog clicks off the surrounding Link (same defensive
          // handling as the delete dialog above).
          onClick={(e) => e.stopPropagation()}
        >
          <DialogHeader>
            <DialogTitle>Stop session?</DialogTitle>
            <DialogDescription>
              This terminates the running session for <span className="font-medium">{label}</span>{" "}
              and stops its runner. The conversation and its history are kept.
            </DialogDescription>
          </DialogHeader>
          {stopSession.isError && (
            <p className="text-ui text-destructive" role="alert">
              Couldn't stop the session
              {stopSession.error instanceof Error && stopSession.error.message
                ? `: ${stopSession.error.message}`
                : " — it may still be running"}
              . Try again in a moment.
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setStopOpen(false)}
              disabled={stopSession.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              data-testid="stop-session-confirm"
              onClick={() =>
                stopSession.mutate(conversation.id, { onSuccess: () => setStopOpen(false) })
              }
              loading={stopSession.isPending}
              componentId="sidebar.conversation.stop"
            >
              Stop session
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  );
}

/**
 * Hover flyout body for a pinned, project-owned conversation row.
 *
 * Pinning lifts a session out of its project folder into the flat "Pinned"
 * section, dropping the visual project cue the folder provided. Hovering the
 * row surfaces it again: the session title, project name, and optional branch.
 * Mirrors {@link AgentHoverCard}'s Cursor-style placement (right / top-aligned)
 * and the muted, small-icon foreground used elsewhere in the sidebar.
 */
function PinnedProjectFlyoutContent({
  title,
  projectName,
  gitBranch,
}: {
  title: string;
  projectName: string;
  gitBranch: string | null;
}) {
  return (
    <HoverCardContent
      side="right"
      align="start"
      sideOffset={8}
      className="w-64"
      data-testid="pinned-project-flyout"
    >
      {/* Titles have no length cap (server + rename input are unbounded), so
          clamp to 3 wrapped lines to keep the card tidy — full text stays in
          the DOM. */}
      <p className="sidebar-compact-text line-clamp-3 font-medium">{title}</p>
      <p className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground">
        <FolderIcon className="size-3.5 shrink-0" />
        <span className="truncate">{projectName}</span>
      </p>
      {gitBranch && (
        <p
          data-testid="pinned-project-flyout-branch"
          className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
        >
          <GitBranchIcon aria-hidden className="size-3.5 shrink-0" />
          <span className="truncate">{gitBranch}</span>
        </p>
      )}
    </HoverCardContent>
  );
}

// ── ProjectFolderActions ──────────────────────────────────────────────────────

/** Fine-hover shortcuts for project actions and starting a pre-filed session. */
function ProjectFolderActions({
  projectName,
  onNavigate,
  actions,
}: {
  projectName: string;
  /** Plain-left-click nav handler — closes the mobile overlay so the
      pre-filed new-session page isn't left hidden behind the sidebar. */
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
  actions: ProjectFolderMenuActions;
}) {
  return (
    // gap-0.5 (2px) between the pencil and kebab mirrors the session row's
    // pin↔kebab spacing, so the two icon columns line up across row types.
    <div className="flex items-center gap-0.5">
      {/* A redundant shortcut for the kebab's always-present "New session"
          item, so it is genuinely absent (not just clipped) wherever no fine
          hover pointer can reveal it: on touch, folding fully into the kebab
          instead of leaving a duplicate focus stop for keyboard/AT users. On
          hover+fine it is display-flex and revealed on hover or keyboard focus
          by the overlay's opacity. */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            asChild
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`New session in ${projectName}`}
            data-testid="project-new-session"
            className="hidden text-muted-foreground [@media((hover:hover)_and_(pointer:fine))]:flex"
          >
            <Link
              to={`/?project=${encodeURIComponent(projectName)}`}
              onClick={(e) => {
                // Keep the click off the folder's collapse toggle, then run the
                // shared nav handler (closes the sidebar overlay on mobile).
                e.stopPropagation();
                onNavigate(e);
              }}
            >
              <SquarePenIcon className="size-3.5" data-icon-size="14" />
            </Link>
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New session in project</TooltipContent>
      </Tooltip>
      <ProjectFolderMenu projectName={projectName} onNavigate={onNavigate} actions={actions} />
    </div>
  );
}

// ── ProjectFolderMenu ─────────────────────────────────────────────────────────

/** The menu body shared by the project-folder kebab and context menu. */
function ProjectFolderMenuItems({
  components: C,
  projectName,
  onNavigate,
  actions,
}: {
  components: MenuComponents;
  projectName: string;
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
  actions: ProjectFolderMenuActions;
}) {
  useEffect(() => {
    // Config loading follows the Radix content lifecycle; do not force-mount it.
    actions.onMenuOpen();
    return actions.onMenuClose;
  }, [actions]);

  return (
    <>
      <C.Item asChild data-testid="project-new-session-menu">
        <Link
          to={`/?project=${encodeURIComponent(projectName)}`}
          onClick={(e) => {
            e.stopPropagation();
            onNavigate(e);
          }}
        >
          <SquarePenIcon className="size-3.5" />
          New session
        </Link>
      </C.Item>
      <C.Item data-testid="rename-project" onSelect={actions.openRename}>
        <PencilIcon className="size-3.5" />
        Rename project
      </C.Item>
      <C.Item data-testid="project-settings" onSelect={actions.openSettings}>
        <Settings2Icon className="size-3.5" />
        Project settings
      </C.Item>
      <C.Item data-testid="delete-project" variant="destructive" onSelect={actions.openDelete}>
        <Trash2Icon className="size-3.5" />
        Delete project
      </C.Item>
    </>
  );
}

interface ProjectFolderMenuActions {
  openRename: () => void;
  openSettings: () => void;
  openDelete: () => void;
  onMenuOpen: () => void;
  onMenuClose: () => void;
}

/** Owns the dialogs and mutations shared by both project-folder menus. */
function useProjectFolderMenu(
  projectName: string,
  projectId: string | null,
  icon?: string | null,
): { actions: ProjectFolderMenuActions; dialogs: ReactNode } {
  const [menuOpen, setMenuOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const [renameValue, setRenameValue] = useState(projectName);
  // The icon staged in the rename modal, committed only on Confirm:
  //   undefined = untouched (show the saved icon), string = a picked emoji,
  //   null = staged removal. Reset to `undefined` each time the modal opens.
  const [pendingIcon, setPendingIcon] = useState<string | null | undefined>(undefined);
  const deleteProject = useDeleteProject();
  const renameProject = useRenameProject();
  const updateConfig = useUpdateProjectConfig();
  // Fetch the full config only while the menu or rename modal is open, so we can
  // merge the icon onto the other stored defaults (host / workspace / agent)
  // without a per-folder request on every sidebar render — and without wiping
  // those defaults on save.
  const { data: iconConfig, isError: iconConfigError } = useProjectConfig(
    menuOpen || renameOpen ? projectId : null,
  );
  // The config PATCH replaces the whole blob, so an ICON save must merge onto a
  // fully-loaded config or it silently wipes the other defaults. "Ready" means
  // the config actually resolved (`!== undefined` — `isLoading` alone is false
  // on a query *error* too, leaving no data to merge onto) — except a
  // label-only folder (`projectId === null`), whose base is legitimately `{}`.
  // This gates only the icon path; renaming the name never needs the config.
  const configReady = projectId === null || iconConfig !== undefined;
  const savedIcon = iconConfig !== undefined ? iconConfig?.icon : icon;
  // What the modal's tile shows: the staged pick when touched, else the saved
  // icon. `null` (staged removal) renders as the empty folder.
  const displayIcon = pendingIcon !== undefined ? pendingIcon : savedIcon;

  const actions = useMemo<ProjectFolderMenuActions>(
    () => ({
      openRename: () => {
        setRenameValue(projectName);
        setPendingIcon(undefined);
        setRenameOpen(true);
      },
      openSettings: () => setSettingsOpen(true),
      openDelete: () => setDeleteOpen(true),
      onMenuOpen: () => setMenuOpen(true),
      onMenuClose: () => setMenuOpen(false),
    }),
    [projectName],
  );

  const dialogs = (
    <>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent
          onClick={(e) => e.stopPropagation()}
          // emoji-mart preventDefaults the pointer event, so Radix's own
          // outside-dismissal never fires for clicks elsewhere in the modal.
          // Catch them in the capture phase and close the picker ourselves,
          // unless the pointer is inside the picker or on its trigger tile.
          onPointerDownCapture={(e) => {
            if (!emojiOpen) return;
            const target = e.target as Element;
            if (
              target.closest('[data-slot="popover-content"]') ||
              target.closest('[data-testid="rename-project-icon"]')
            )
              return;
            setEmojiOpen(false);
          }}
        >
          <DialogHeader>
            <DialogTitle>Rename project</DialogTitle>
          </DialogHeader>
          {/* A <form> so Enter in the input submits natively (Radix Dialog
              doesn't wrap children in one) instead of relying on a manual
              key handler + button lookup. */}
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              const newName = renameValue.trim();
              const nameChanged = newName !== "" && newName !== projectName;
              const iconChanged = pendingIcon !== undefined && pendingIcon !== (savedIcon ?? null);
              // Only the icon write needs a loaded config to merge onto; a
              // name-only rename must proceed even if the config fetch failed.
              if (iconChanged && !configReady) return;
              try {
                // Name first: it promotes a label-only folder (creating the
                // first-class row) and reconciles members. Capture the resolved
                // id so the icon write below targets that row — passing the
                // stale render-time `null` would make the config write try to
                // create the project a second time and 409 on the duplicate.
                let targetId = projectId;
                if (nameChanged) {
                  targetId = await renameProject.mutateAsync({
                    id: projectId,
                    oldName: projectName,
                    newName,
                  });
                }
                if (iconChanged) {
                  const next = { ...(iconConfig ?? {}) };
                  if (pendingIcon === null) delete next.icon;
                  else next.icon = pendingIcon;
                  await updateConfig.mutateAsync({
                    id: targetId,
                    name: nameChanged ? newName : projectName,
                    config: next,
                  });
                }
              } catch {
                // Errors surface via the mutation state below. A rename that
                // lands before a failed icon write is already persisted; only
                // the icon needs retrying.
                return;
              }
              setRenameOpen(false);
              setMenuOpen(false);
            }}
          >
            {/* One combined control: emoji tile (left) + name (right) share a
                single border, so it reads as a single input. The tile opens a
                picker popover; the pick is staged and committed on Confirm. */}
            <div className="flex items-stretch overflow-hidden rounded-lg border border-input">
              <Popover open={emojiOpen} onOpenChange={setEmojiOpen}>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    aria-label="Change project icon"
                    data-testid="rename-project-icon"
                    disabled={!configReady}
                    className={cn(
                      "flex size-[38px] shrink-0 cursor-pointer items-center justify-center outline-none transition-colors disabled:cursor-default disabled:opacity-50",
                      displayIcon ? "bg-muted" : "bg-tag-pink",
                    )}
                  >
                    {displayIcon ? (
                      <span className="text-xl leading-none">{displayIcon}</span>
                    ) : (
                      <SmilePlusIcon className="size-4 text-brand-accent" />
                    )}
                  </button>
                </PopoverTrigger>
                <PopoverContent
                  align="start"
                  // Publish the collision-aware available viewport height (Radix
                  // exposes it as a CSS var), less the optional "Remove icon"
                  // header and capped at the picker's natural size, so the
                  // .emoji-picker-popover rule in index.css shrinks emoji-mart to
                  // fit — it then scrolls its grid internally (nav + search
                  // pinned) instead of clipping on short screens.
                  collisionPadding={8}
                  style={
                    {
                      "--emoji-picker-height": `min(420px, calc(var(--radix-popover-content-available-height) - ${displayIcon ? "38px" : "0px"}))`,
                    } as CSSProperties
                  }
                  className="emoji-picker-popover flex max-h-[var(--radix-popover-content-available-height)] w-auto flex-col overflow-hidden p-0"
                  // The rename Dialog's scroll lock (react-remove-scroll)
                  // preventDefaults wheel events over the picker — it can't see
                  // emoji-mart's scroll region inside shadow DOM. Stop the wheel
                  // from reaching the document-level lock so the grid scrolls.
                  onWheel={(e) => e.stopPropagation()}
                  // Nested in the rename Dialog, emoji-mart's own focus handling
                  // swallows Radix's default outside-pointer dismissal, so a
                  // click elsewhere in the modal wouldn't close the picker.
                  // Close it explicitly on any outside interaction.
                >
                  {displayIcon ? (
                    <div className="shrink-0 border-b p-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="w-full justify-start"
                        data-testid="rename-project-remove-icon"
                        onClick={() => {
                          setPendingIcon(null);
                          setEmojiOpen(false);
                        }}
                      >
                        <Trash2Icon className="size-3.5" />
                        Remove icon
                      </Button>
                    </div>
                  ) : null}
                  <EmojiPicker
                    onSelect={(native) => {
                      setPendingIcon(native);
                      setEmojiOpen(false);
                    }}
                  />
                </PopoverContent>
              </Popover>
              <input
                className="w-full bg-transparent px-3 py-2 text-ui outline-none"
                value={renameValue}
                onChange={(e) => setRenameValue(e.target.value)}
              />
            </div>
            {iconConfigError && (
              <p className="text-ui text-destructive" role="alert">
                Couldn&apos;t load this project&apos;s icon settings. You can still rename it;
                changing the icon is unavailable until this loads.
              </p>
            )}
            {(renameProject.isError || updateConfig.isError) && (
              <p className="text-ui text-destructive" role="alert">
                {((renameProject.error ?? updateConfig.error) as Error).message}
              </p>
            )}
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setRenameOpen(false)}
                disabled={renameProject.isPending || updateConfig.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                data-testid="rename-project-confirm"
                loading={renameProject.isPending || updateConfig.isPending}
                disabled={renameValue.trim() === ""}
                componentId="sidebar.project.rename"
              >
                Confirm
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
      <ProjectSettingsDialog
        open={settingsOpen}
        onOpenChange={(o) => {
          setSettingsOpen(o);
          if (!o) setMenuOpen(false);
        }}
        projectId={projectId}
        projectName={projectName}
      />
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>Delete project?</DialogTitle>
            <DialogDescription>
              This deletes the project{" "}
              <span className="rounded bg-muted px-1 py-0.5 font-mono text-[0.95em] break-all">
                {projectName}
              </span>{" "}
              and archives <span className="font-medium">all of its sessions</span>. Their history
              is kept. You can find and restore them anytime from Settings.
            </DialogDescription>
          </DialogHeader>
          {deleteProject.isError && (
            <p className="text-ui text-destructive" role="alert">
              Some sessions couldn't be archived (you may not own them); the rest were archived.
            </p>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setDeleteOpen(false)}
              disabled={deleteProject.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              loading={deleteProject.isPending}
              onClick={() => {
                deleteProject.mutate(
                  { id: projectId, name: projectName },
                  {
                    onSuccess: () => {
                      setDeleteOpen(false);
                      setMenuOpen(false);
                    },
                  },
                );
              }}
            >
              Delete project
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );

  return { actions, dialogs };
}

function ProjectFolderMenu({
  projectName,
  onNavigate,
  actions,
}: {
  projectName: string;
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
  actions: ProjectFolderMenuActions;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {/* Revealed on hover for fine hover pointers at any width; otherwise
            sr-only so touch and assistive tech keep a focusable, announced
            trigger for the same actions the long-press menu also opens. */}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`Project actions for ${projectName}`}
          data-testid="project-actions"
          className="sr-only text-muted-foreground focus-visible:not-sr-only [@media((hover:hover)_and_(pointer:fine))]:not-sr-only [@media((hover:hover)_and_(pointer:fine))]:flex"
          onClick={(e) => e.stopPropagation()}
        >
          <MoreHorizontalIcon className="size-3.5" data-icon-size="14" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        <ProjectFolderMenuItems
          components={dropdownBundle}
          projectName={projectName}
          onNavigate={onNavigate}
          actions={actions}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ── ProjectPickerMenu ─────────────────────────────────────────────────────────

/**
 * Project picker rendered as the body of a {@link DropdownMenuSubContent}.
 *
 * Lives inside the kebab menu's submenu flyout rather than a separate popover —
 * that avoids the open/dismiss race that made a standalone popover flash open
 * and vanish. The search / new-project inputs stop key events from bubbling so
 * the menu's built-in typeahead and arrow-key navigation don't hijack typing.
 */
function ProjectPickerMenu({
  components: C,
  currentProject,
  onSelect,
}: {
  components: MenuComponents;
  currentProject: string | null;
  onSelect: (project: string) => void;
}) {
  const { data: projects = [] } = useProjects();
  const [search, setSearch] = useState("");

  const trimmed = search.trim();
  const filtered = trimmed
    ? projects.filter((p) => p.name.toLowerCase().includes(trimmed.toLowerCase()))
    : projects;
  // Offer create only when the typed name isn't already an exact project.
  const canCreate =
    trimmed.length > 0 && !projects.some((p) => p.name.toLowerCase() === trimmed.toLowerCase());
  const currentProjectIcon = projects.find((p) => p.name === currentProject)?.icon;

  // Keep keystrokes inside the inputs from reaching the menu's typeahead /
  // navigation handlers (which would otherwise steal letters and arrows).
  const swallowKeys = (e: KeyboardEvent<HTMLInputElement>) => e.stopPropagation();

  return (
    <>
      {/* Combobox-style search: a leading magnifier inside a borderless input,
          with a divider beneath separating it from the results. */}
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <input
          className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          placeholder="Search or create project"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={swallowKeys}
        />
      </div>
      <div className="max-h-48 overflow-y-auto">
        {filtered.map((p) => (
          <C.Item
            key={p.name}
            className="px-2 py-1"
            textValue={p.name}
            onSelect={() => onSelect(p.name)}
          >
            <ProjectRowIcon icon={p.icon} />
            <span className="flex-1 truncate text-left">{p.name}</span>
            {currentProject === p.name && (
              <CheckMarkIcon className="size-3.5 shrink-0 text-primary" />
            )}
          </C.Item>
        ))}
        {filtered.length === 0 && !canCreate && (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">No projects yet.</p>
        )}
      </div>
      {canCreate && (
        <div className="border-t pt-1">
          <C.Item className="px-2 py-1" onSelect={() => onSelect(trimmed)}>
            <PlusIcon className="size-3.5 shrink-0 text-muted-foreground" />
            Create{" "}
            <span className="truncate rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {trimmed}
            </span>
          </C.Item>
        </div>
      )}
      {currentProject && (
        <div className="border-t pt-1">
          <C.Item
            className="px-2 py-1"
            textValue={`Remove from ${currentProject}`}
            onSelect={() => onSelect("")}
          >
            <ProjectRowIcon icon={currentProjectIcon} />
            Remove from{" "}
            <span className="rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {currentProject}
            </span>
          </C.Item>
        </div>
      )}
    </>
  );
}

// ── ConversationEditRow ──────────────────────────────────────────────────────

interface ConversationEditRowProps {
  initialTitle: string;
  onCommit: (title: string) => void;
  onCancel: () => void;
}

/**
 * Inline-edit shell for a conversation row.
 *
 * Auto-focuses on mount and selects the whole title so the user can
 * start typing to replace. Enter commits, Escape cancels, blur
 * commits — matches the spec's "lose focus or press enter" wording.
 * The blur-commits-on-Escape case is avoided by clearing the value
 * with the dedicated cancel handler before blur fires.
 */
function ConversationEditRow({ initialTitle, onCommit, onCancel }: ConversationEditRowProps) {
  const [value, setValue] = useState(initialTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  // Set when the user explicitly cancels (Escape or X click); blur
  // checks this so we don't double-fire onCommit with the unedited
  // value when the input loses focus as part of unmounting.
  const cancelledRef = useRef(false);
  // Tracks an active IME composition (e.g. Japanese conversion) so the Enter
  // that confirms a candidate doesn't commit the rename. Mirrors the chat
  // composer guard (#132/#243).
  const isComposingRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (isImeCompositionKeyEvent(e, isComposingRef.current)) return;
    if (e.key === "Enter") {
      e.preventDefault();
      onCommit(value);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      cancelledRef.current = true;
      onCancel();
    }
  }

  function handleBlur() {
    if (cancelledRef.current) return;
    onCommit(value);
  }

  return (
    // Match the interactive row's responsive box metrics so entering edit mode
    // doesn't shift the list. pl-1 + the input's px-1 align with row titles.
    <div className="sidebar-compact-text flex h-8 items-center gap-1 rounded-[var(--radius-otto-sm)] bg-muted pr-1 pl-1 md:h-7">
      <input
        ref={inputRef}
        type="text"
        maxLength={USER_SESSION_TITLE_MAX_CHARS}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onCompositionStart={() => {
          isComposingRef.current = true;
        }}
        onCompositionEnd={() => {
          isComposingRef.current = false;
        }}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        data-testid="rename-conversation-input"
        className="min-w-0 flex-1 truncate rounded bg-transparent px-1 py-0.5 outline-none md:select-text"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Save rename"
        onMouseDown={(e) => {
          // Prevent the input's blur from firing before the commit.
          e.preventDefault();
        }}
        onClick={() => onCommit(value)}
      >
        <CheckIcon className="size-3.5" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label="Cancel rename"
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          cancelledRef.current = true;
          onCancel();
        }}
      >
        <XIcon className="size-3.5" />
      </Button>
    </div>
  );
}

function BulkActionBar({
  selectedIds,
  allConversations,
  onDeselectAll,
  onExit,
  onProjectAssigned,
}: {
  selectedIds: Set<string>;
  allConversations: Conversation[];
  onDeselectAll: () => void;
  onExit: () => void;
  onProjectAssigned?: (projectName: string) => void;
}) {
  const navigate = useNavigate();
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const bulkArchive = useBulkArchiveConversations();
  const bulkDelete = useBulkDeleteConversations();
  const bulkMove = useBulkMoveToProject();
  const { data: projects = [] } = useProjects();
  const viewerId = useViewerId();

  const selectedConversations = useMemo(
    () => allConversations.filter((c) => selectedIds.has(c.id)),
    [allConversations, selectedIds],
  );

  const ownedSelected = useMemo(
    () => selectedConversations.filter((c) => isOwnedByViewer(c, viewerId)),
    [selectedConversations, viewerId],
  );

  const archivedSelected = useMemo(
    () => ownedSelected.filter((c) => c.archived === true),
    [ownedSelected],
  );

  const nonArchivedSelected = useMemo(
    () => ownedSelected.filter((c) => c.archived !== true),
    [ownedSelected],
  );

  const allSelectedSameArchiveGroup =
    ownedSelected.length > 0 && (archivedSelected.length === 0 || nonArchivedSelected.length === 0);

  const count = selectedIds.size;
  const isBusy = bulkArchive.isPending || bulkDelete.isPending || bulkMove.isPending;

  // Delete acts only on owned rows, so surface that count on the control when it
  // differs from "N selected" — otherwise a mixed-ownership selection (reachable
  // in projects scope, where a folder can hold others' sessions) reads
  // "3 selected" while Delete hits fewer. Used for both the tooltip (visual) and
  // the aria-label (assistive tech). Archive needs no such hint: its gate
  // (`allSelectedSameArchiveGroup`) only enables it when every owned row shares
  // one archive state, and archived rows never appear in a selectable section.
  const deleteLabel =
    ownedSelected.length > 0 && ownedSelected.length !== count
      ? `Delete ${ownedSelected.length}`
      : "Delete";

  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);
  const [moveSearch, setMoveSearch] = useState("");
  // Worktree sessions among the selection each carry one local git branch
  // (git_branch); the delete modal lists them so each branch can be opted
  // into cleanup individually. `branchesToDelete` holds the session ids whose
  // branch the user ticked — default empty (opt-in, matching single-session
  // delete, since branch deletion is irreversible).
  const [branchesToDelete, setBranchesToDelete] = useState<Set<string>>(new Set());

  const worktreeSelected = useMemo(
    () => ownedSelected.filter((c) => c.git_branch),
    [ownedSelected],
  );
  const allBranchesSelected =
    worktreeSelected.length > 0 && worktreeSelected.every((c) => branchesToDelete.has(c.id));
  // Drives the header checkbox's indeterminate ([-]) state: some but not all
  // branches ticked.
  const someBranchesSelected =
    !allBranchesSelected && worktreeSelected.some((c) => branchesToDelete.has(c.id));

  function toggleBranch(id: string, checked: boolean) {
    setBranchesToDelete((prev) => {
      const next = new Set(prev);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function toggleAllBranches() {
    setBranchesToDelete(
      allBranchesSelected ? new Set() : new Set(worktreeSelected.map((c) => c.id)),
    );
  }

  function handleMoveToProject(project: string) {
    const ids = ownedSelected.map((c) => c.id);
    if (ids.length === 0) return;
    bulkMove.mutate(
      { ids, project },
      {
        onSuccess: () => {
          if (project) onProjectAssigned?.(project);
          onExit();
        },
      },
    );
  }

  function handleArchive() {
    if (nonArchivedSelected.length === 0) return;
    // The rows leave the sidebar optimistically (useBulkArchiveConversations
    // flips their cached `archived` flag in onMutate), so this bar unmounts
    // with the selection. Navigate and deselect NOW rather than in a
    // mutate-level callback — a callback on the unmounted observer never fires,
    // and it would carry a stale `activeId` that could jump the user off a
    // session they switched to meanwhile. Mirrors handleDelete.
    if (activeId && nonArchivedSelected.some((c) => c.id === activeId))
      navigate("/", { replace: true });
    onDeselectAll();
    bulkArchive.mutate({ ids: nonArchivedSelected.map((c) => c.id), archived: true });
  }

  function handleUnarchive() {
    if (archivedSelected.length === 0) return;
    onDeselectAll();
    bulkArchive.mutate({ ids: archivedSelected.map((c) => c.id), archived: false });
  }

  function handleDelete() {
    const ids = ownedSelected.map((c) => c.id);
    if (ids.length === 0) return;
    setConfirmDeleteOpen(false);
    // The rows leave the sidebar optimistically, so the selection is already
    // meaningless and the chat surface would be sitting on an id that's about
    // to 404. Both are settled here rather than in a mutate-level callback:
    // this bar unmounts with the selection, and callbacks on an unmounted
    // observer never fire.
    if (activeId && ids.includes(activeId)) navigate("/", { replace: true });
    onDeselectAll();
    bulkDelete.mutate({ ids, deleteBranchIds: branchesToDelete });
  }

  return (
    <>
      <div className="mt-1 mb-1 flex flex-col gap-1.5">
        <div className="flex items-center gap-2 rounded-lg border border-border bg-transparent p-1.5">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="shrink-0"
                aria-label="Exit selection mode"
                data-testid="toggle-selection-mode"
                onClick={onExit}
              >
                <XIcon className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Exit selection</TooltipContent>
          </Tooltip>
          <span className="sidebar-compact-text shrink-0 whitespace-nowrap font-medium">
            {count} selected
          </span>

          <div className="ml-auto flex items-center gap-0.5">
            {!(allSelectedSameArchiveGroup && archivedSelected.length > 0) && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="shrink-0"
                    disabled={isBusy || nonArchivedSelected.length === 0}
                    onClick={handleArchive}
                    aria-label="Archive selected"
                    data-testid="bulk-archive"
                  >
                    {bulkArchive.isPending ? (
                      <Loader2Icon className="size-3.5 animate-spin" />
                    ) : (
                      <ArchiveIcon className="size-3.5" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Archive</TooltipContent>
              </Tooltip>
            )}
            {allSelectedSameArchiveGroup && archivedSelected.length > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="shrink-0"
                    disabled={isBusy}
                    onClick={handleUnarchive}
                    aria-label="Unarchive selected"
                    data-testid="bulk-unarchive"
                  >
                    {bulkArchive.isPending ? (
                      <Loader2Icon className="size-3.5 animate-spin" />
                    ) : (
                      <ArchiveRestoreIcon className="size-3.5" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Unarchive</TooltipContent>
              </Tooltip>
            )}
            <DropdownMenu
              onOpenChange={(open) => {
                if (!open) setMoveSearch("");
              }}
            >
              <Tooltip>
                <TooltipTrigger asChild>
                  <DropdownMenuTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      className="shrink-0"
                      disabled={isBusy || ownedSelected.length === 0}
                      aria-label="Move to project"
                      data-testid="bulk-move-to-project"
                    >
                      {bulkMove.isPending ? (
                        <Loader2Icon className="size-3.5 animate-spin" />
                      ) : (
                        <FolderInputIcon className="size-3.5" />
                      )}
                    </Button>
                  </DropdownMenuTrigger>
                </TooltipTrigger>
                <TooltipContent side="bottom">Move to project</TooltipContent>
              </Tooltip>
              <DropdownMenuContent align="end" className="w-52">
                <div className="flex items-center gap-2 border-b px-2 py-1.5">
                  <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
                  <input
                    className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
                    placeholder="Search projects"
                    value={moveSearch}
                    onChange={(e) => setMoveSearch(e.target.value)}
                    onKeyDown={(e) => e.stopPropagation()}
                  />
                </div>
                <div className="max-h-48 overflow-y-auto">
                  {(moveSearch
                    ? projects.filter((p) =>
                        p.name.toLowerCase().includes(moveSearch.toLowerCase()),
                      )
                    : projects
                  ).map((p) => (
                    <DropdownMenuItem
                      key={p.name}
                      className="px-2 py-1"
                      onSelect={() => handleMoveToProject(p.name)}
                    >
                      <span className="flex-1 truncate text-left">{p.name}</span>
                    </DropdownMenuItem>
                  ))}
                  {projects.length === 0 && (
                    <p className="px-2 py-1.5 text-sm text-muted-foreground">No projects yet.</p>
                  )}
                </div>
              </DropdownMenuContent>
            </DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  className={cn("shrink-0", ownedSelected.length > 0 && "text-destructive")}
                  disabled={isBusy || ownedSelected.length === 0}
                  onClick={() => setConfirmDeleteOpen(true)}
                  aria-label={deleteLabel}
                  data-testid="bulk-delete"
                >
                  {bulkDelete.isPending ? (
                    <Loader2Icon className="size-3.5 animate-spin" />
                  ) : (
                    <Trash2Icon className="size-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">{deleteLabel}</TooltipContent>
            </Tooltip>
          </div>
        </div>

        {(bulkArchive.isError || bulkDelete.isError || bulkMove.isError) && (
          <p className="text-sm text-destructive" role="alert">
            Some actions failed. Retry or dismiss.
          </p>
        )}
      </div>

      <Dialog
        open={confirmDeleteOpen}
        onOpenChange={(open) => {
          setConfirmDeleteOpen(open);
          // Reset the branch selection on close so it doesn't carry over to
          // the next delete.
          if (!open) setBranchesToDelete(new Set());
        }}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Delete {ownedSelected.length} session(s)?</DialogTitle>
            <DialogDescription>
              This will permanently delete the selected sessions and all their history. This cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          {worktreeSelected.length > 0 && (
            <div className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
              <p className="text-sm text-muted-foreground">
                Optionally delete the local git branches for these worktree sessions. These actions
                are <span className="font-semibold text-destructive">irreversible</span>.
              </p>
              <div className="max-h-56 overflow-y-auto">
                <table className="w-full border-collapse text-left text-ui">
                  <thead>
                    <tr className="border-b border-destructive/20 text-sm text-muted-foreground">
                      <th scope="col" className="w-8 py-1.5 pr-2 font-medium">
                        <input
                          type="checkbox"
                          ref={(el) => {
                            if (el) el.indeterminate = someBranchesSelected;
                          }}
                          checked={allBranchesSelected}
                          onChange={toggleAllBranches}
                          aria-label="Select all branches"
                          data-testid="bulk-delete-branch-toggle-all"
                          className="mt-1 size-4 shrink-0 accent-destructive"
                        />
                      </th>
                      <th scope="col" className="py-1.5 pr-3 font-medium">
                        Branch
                      </th>
                      <th scope="col" className="py-1.5 font-medium">
                        Session
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {worktreeSelected.map((c) => (
                      <tr key={c.id} className="align-top">
                        <td className="py-2 pr-2">
                          <input
                            type="checkbox"
                            data-testid="bulk-delete-branch-checkbox"
                            checked={branchesToDelete.has(c.id)}
                            onChange={(e) => toggleBranch(c.id, e.target.checked)}
                            aria-label={`Delete branch ${c.git_branch}`}
                            className="mt-0.5 size-4 shrink-0 cursor-pointer accent-destructive"
                          />
                        </td>
                        <td className="py-2 pr-3">
                          <span className="flex items-start gap-1.5">
                            <GitBranchIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                            <code className="break-all rounded bg-muted px-1 py-0.5 text-sm">
                              {c.git_branch}
                            </code>
                          </span>
                        </td>
                        <td className="py-2 text-sm text-muted-foreground">
                          <span className="line-clamp-2 break-all">
                            {conversationDisplayLabel(c)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setConfirmDeleteOpen(false);
                setBranchesToDelete(new Set());
              }}
              disabled={bulkDelete.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDelete}
              disabled={bulkDelete.isPending}
            >
              Delete {ownedSelected.length} session(s)
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * Returns true on mobile viewports (below the `md` breakpoint of
 * 768px). Used to gate the auto-close-on-navigation behavior — on
 * mobile the sidebar is a full-screen overlay so dismissing on action
 * is what reveals the destination; on desktop the sidebar pushes content
 * aside and staying open is more useful.
 *
 * SSR-safe (returns false when window is undefined).
 */
export function isMobileViewport(): boolean {
  if (typeof window === "undefined") return false;
  return !window.matchMedia("(min-width: 768px)").matches;
}

// Default collapse state: every section (Pinned / Projects / Chats / Shared)
// starts expanded. Archived no longer lives in the sidebar (it's on the
// Settings page). Once the user toggles any header, the stored array (even an
// empty one) becomes the preference and persists across reloads.
const DEFAULT_COLLAPSED_SIDEBAR_SECTIONS: string[] = [];

function readCollapsedSidebarSections(): string[] {
  if (typeof window === "undefined") return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
  try {
    const raw = window.localStorage.getItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY);
    if (!raw) return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
    return parsed.filter((value): value is string => typeof value === "string");
  } catch {
    // Same contract as pins: corrupt storage means "back to defaults",
    // never a broken sidebar.
    return DEFAULT_COLLAPSED_SIDEBAR_SECTIONS;
  }
}

function writeCollapsedSidebarSections(titles: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY, JSON.stringify(titles));
  } catch {
    // Collapse state is a local navigation preference; losing it is fine.
  }
}

// Project folders default to collapsed, so the persisted set is the EXPANDED
// names (empty by default = every project starts collapsed).
function readExpandedProjectSections(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((value): value is string => typeof value === "string");
  } catch {
    return [];
  }
}

function writeExpandedProjectSections(names: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(EXPANDED_PROJECT_SECTIONS_STORAGE_KEY, JSON.stringify(names));
  } catch {
    // Same as collapse state — a lost local preference is harmless.
  }
}
