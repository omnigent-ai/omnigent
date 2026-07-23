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
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertTriangleIcon,
  ArchiveIcon,
  ArchiveRestoreIcon,
  CheckIcon,
  CheckIcon as CheckMarkIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CircleStopIcon,
  FolderIcon,
  FolderInputIcon,
  FolderMinusIcon,
  FolderOpenIcon,
  GitBranchIcon,
  InboxIcon,
  ListChecksIcon,
  LaptopIcon,
  Loader2Icon,
  MailIcon,
  Maximize2Icon,
  Minimize2Icon,
  MoreHorizontalIcon,
  PanelRightOpenIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  SearchIcon,
  SettingsIcon,
  ShareIcon,
  SquareIcon,
  SquareCheckIcon,
  SquarePenIcon,
  Trash2Icon,
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
import { Link, useLocation, useNavigate, useParams } from "@/lib/routing";
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
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  type Conversation,
  useArchiveConversation,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useProjects,
  useProjectSessions,
  useConversations,
  useMoveToProject,
  useDeleteProject,
  useRenameProject,
  PROJECT_LABEL_KEY,
  usePinnedConversationBackfill,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
} from "@/hooks/useConversations";
import { useHosts, type Host } from "@/hooks/useHosts";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode, sandboxOptionLabel } from "@/lib/capabilities";
import { relativeTime } from "@/lib/relativeTime";
import { showToast } from "@/components/ui/toast";
import { PermissionsModal } from "@/components/PermissionsModal";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useActiveRootSessionId } from "@/hooks/useSession";
import { useCommentInbox } from "@/hooks/useCommentInbox";
import { sumPendingApprovals } from "@/lib/inbox";
import { isSessionStoppable } from "@/lib/sessionStop";
import { getCurrentUserId, resolveIdentity } from "@/lib/identity";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { getSessionState, type SessionState } from "@/hooks/useSessionState";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  markConversationUnread,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { cn } from "@/lib/utils";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizableSidebar } from "@/hooks/useResizableSidebar";
import { useSessionSwitchHotkey } from "@/hooks/useSessionSwitchHotkey";
import { usePinnedSessionHotkeys } from "@/hooks/usePinnedSessionHotkeys";
import { isCurrentServerLocal } from "@/lib/serverOrigin";
import { NewProjectButton } from "./NewProjectButton";
import { SettingsSidebarBody, useSettingsRoute, useTrackSettingsReturn } from "./settingsNav";
import {
  type ActiveChatOverride,
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  computeNextActiveOverride,
  conversationDisplayLabel,
  dedupeConversationsById,
  EXPANDED_PROJECT_SECTIONS_STORAGE_KEY,
  migratePinnedConversationIds,
  normalizePinnedConversationIds,
  orderByPinnedSequence,
  PINNED_CONVERSATION_IDS_STORAGE_KEY,
  resolveSidebarDrop,
  type SidebarDropTarget,
  sortByUpdatedAtDesc,
  togglePinnedConversationId,
} from "./sidebarNav";

// Positioning for a row's trailing session-state badge. On desktop the badge
// fades out on hover/focus so the pin + kebab controls can take its place; on
// mobile it sits left of the always-visible controls (right-[4.5rem]).
const SESSION_STATE_SLOT_CLASS =
  "-translate-y-1/2 pointer-events-none absolute top-1/2 right-[4.5rem] flex h-5 items-center transition-opacity md:right-2 md:group-hover:opacity-0 md:group-has-[:focus-visible]:opacity-0 md:group-has-[[aria-expanded=true]]:opacity-0";

// Highlight applied to a drop target while a draggable session hovers it: a
// subtle background tint — no border, no shadow. Keyed on --primary like the
// row-selection highlight in this file, at /5 (half the original /10) so it's a
// gentler gray in light mode (a gentler glow in dark mode) and reads as "active
// area" without the heavy fill. Pair with `transition-colors` so it eases in.
const SIDEBAR_HOVER_HIGHLIGHT =
  "hover:bg-[var(--sidebar-hover)] hover:text-[var(--sidebar-active-foreground)]";
const SIDEBAR_ACTIVE_HIGHLIGHT =
  "bg-[var(--sidebar-active)] text-[var(--sidebar-active-foreground)]";
const DROP_TARGET_HIGHLIGHT = SIDEBAR_ACTIVE_HIGHLIGHT;

// Maps a first-class project id → its name, provided once at the list level so
// each row resolves its ``project_id`` to a folder name without its own
// ``useProjects()`` subscription. Keeps row renders O(1) and avoids spinning up
// a query observer per row (which would also re-run on every project mutation).
const ProjectNamesContext = createContext<Map<string, string>>(new Map());
const HostsByIdContext = createContext<ReadonlyMap<string, Host>>(new Map());

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
 * Which session tab the sidebar is showing. ``"mine"`` is the viewer's own
 * sessions (the Pinned / Projects / Chats structure); ``"shared"`` is the flat
 * list of sessions others have shared with the viewer. The split mirrors
 * :func:`isOwnedByViewer`.
 */
type SidebarTab = "mine" | "shared";

interface SidebarProps {
  open: boolean;
  onClose: () => void;
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
function useActiveNavItem(): { isNewChatPage: boolean; isInboxPage: boolean } {
  const { conversationId: activeConversationId } = useParams<{ conversationId: string }>();
  const isInboxPage = useLocation().pathname.split("/").filter(Boolean).at(-1) === "inbox";
  // Exclude inbox: it also has no `:conversationId`, so it would otherwise
  // light up the "New session" button.
  const isNewChatPage = activeConversationId == null && !isInboxPage;
  return { isNewChatPage, isInboxPage };
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

export function Sidebar({ open, onClose, dragProgress = null, onOpenSearch }: SidebarProps) {
  const [pinnedConversationIds, setPinnedConversationIds] = useState(readPinnedConversationIds);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // Which session tab is shown. "mine" (default) keeps the full Pinned /
  // Projects / Chats structure; "shared" is a flat list of sessions others
  // shared with the viewer.
  const [activeTab, setActiveTab] = useState<SidebarTab>("mine");
  // The "Shared with me" tab only makes sense when sessions can be shared with
  // other people at all — i.e. a multi-user server. A loopback-only local
  // server has just the one user (mirrors the disabled Share affordance; see
  // `isCurrentServerLocal` and AppShell's `shareDisabled`), so hide the tabs
  // and always show the viewer's own sessions there.
  const multiUser = !isCurrentServerLocal();

  const lastSelectedIdRef = useRef<string | null>(null);
  const getVisibleIdsRef = useRef<() => string[]>(() => []);
  const getVisibleConversationsRef = useRef<() => Conversation[]>(() => []);
  const [visibleConversationCount, setVisibleConversationCount] = useState(0);

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

  const selectAll = useCallback((conversations: Conversation[]) => {
    setSelectedIds(new Set(conversations.map((c) => c.id)));
  }, []);

  const deselectAll = useCallback(() => {
    setSelectedIds(new Set());
  }, []);

  const exitSelectionMode = useCallback(() => {
    setSelectionMode(false);
    setSelectedIds(new Set());
    lastSelectedIdRef.current = null;
  }, []);

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
  const { isNewChatPage, isInboxPage } = useActiveNavItem();

  // On /settings the card keeps its chrome but swaps the conversation list
  // for the settings section nav (see settingsNav.tsx) — entering settings
  // shouldn't replace the whole sidebar.
  const { inSettings } = useSettingsRoute();
  // Remember the pre-settings location so "Back to Omnigent" returns to the
  // conversation the user was viewing, not the home page. Tracked here since
  // the sidebar stays mounted across the transition into settings.
  useTrackSettingsReturn();

  // Sync pinned ids to localStorage whenever state changes. Keeping
  // the write here (instead of inside the state updater) preserves the
  // purity contract of React updaters — important under StrictMode,
  // which may invoke updaters twice.
  useEffect(() => {
    writePinnedConversationIds(pinnedConversationIds);
  }, [pinnedConversationIds]);

  const togglePinnedConversation = useCallback((conversationId: string) => {
    setPinnedConversationIds((prev) => togglePinnedConversationId(prev, conversationId));
  }, []);

  // Desktop-only drag-to-resize, mirroring the right rail. The width is
  // exposed as a CSS variable consumed by the ``md:w-[var(--sidebar-width)]``
  // class so it only applies on desktop — on mobile the sidebar is a
  // full-screen overlay (``fixed inset-0``) and the variable is ignored.
  const { width: sidebarWidth, handleProps: resizeHandleProps } = useResizableSidebar();

  // While the iOS edge-swipe is dragging, the overlay is on-screen and
  // interactive even though `open` hasn't flipped yet — treat a live drag as
  // visually open so it isn't `inert`/`aria-hidden` mid-gesture.
  const dragging = dragProgress != null;
  const effectiveOpen = open || dragging;

  return (
    <aside
      aria-label="Conversations"
      className={cn(
        // Base: bg + flex column. No transition — expand/collapse snaps
        // instantly (animating the width also lagged drag-to-resize).
        // conversations-sidebar only matters under the macOS Electron
        // shell, where it pushes the card below the traffic lights
        // (see the [data-electron-mac] rules in index.css).
        "conversations-sidebar flex flex-col bg-card md:select-none",
        // Mobile (default): fixed full-screen overlay, slide via
        // translate-x. Stays edge-to-edge — the floating-card
        // treatment below is desktop-only.
        // bg-card-solid (opaque): the overlay sits on top of the chat, and
        // WebKit drops the glass rule's backdrop-filter once a Radix popper
        // opens (and never repaints it), letting the chat bleed through the
        // 60%-alpha glass --card. Desktop keeps the translucent bg-card —
        // there the sidebar pushes content aside, so nothing sits behind it.
        "max-md:bg-card-solid",
        "fixed inset-0 z-50",
        // Mobile only: animate the slide so the iOS edge-swipe settles
        // smoothly on release. Suppressed inline while a drag is live (the
        // overlay must track the finger 1:1). Scoped to transform so it can't
        // re-introduce the width-animation lag the base comment warns about,
        // and gated to mobile so the desktop floating card is unaffected.
        "max-md:transition-transform max-md:duration-200 max-md:ease-out",
        effectiveOpen ? "translate-x-0" : "-translate-x-full",
        // Desktop: a floating card. Detached from the window edges by a
        // margin, rounded, and lifted off the bg-sidebar canvas with a
        // full border + shadow. Width (the user-resizable variable) animates
        // →0 to push main; when closed the margin/border collapse too so
        // nothing lingers.
        "md:relative md:inset-auto md:translate-x-0 md:overflow-hidden",
        open
          ? "md:m-2 md:w-[var(--sidebar-width)] md:rounded-[var(--radius-otto-md)] md:border md:border-border"
          : "md:m-0 md:w-0 md:border-0",
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
          when closed also keeps it from being draggable while collapsed. */}
      <div
        {...resizeHandleProps}
        className="absolute inset-y-0 right-0 z-10 hidden w-1 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50 md:block"
      />
      {inSettings ? (
        <SettingsSidebarBody onNavClick={onNavClick} onClose={onClose} />
      ) : (
        <>
          <div className="mt-1 flex h-12 shrink-0 items-center justify-between px-4">
            {/* Brand mark doubles as the "home" affordance: clicking it
            returns to `/`, the new-session composer. Without this there
            is no way back to the landing composer once you're inside a
            session. Reuses onNavClick so a plain primary click closes
            the sidebar on mobile (where it's a full-screen overlay) but
            modifier/middle clicks still open `/` in a new tab. */}
            <Link
              to="/"
              onClick={onNavClick}
              className="rounded-none transition-opacity duration-200 ease-[var(--ease-otto)] hover:opacity-70"
            >
              <img
                src={omnigentWordmark}
                alt="Omnigent"
                data-testid="sidebar-wordmark"
                className="h-[15px] w-auto shrink-0 dark:invert"
              />
            </Link>
            <div className="flex items-center gap-1" data-testid="sidebar-header-actions">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Search"
                    onClick={() => onOpenSearch?.()}
                    className="size-6 rounded-sm text-muted-foreground hover:text-foreground"
                    data-testid="sidebar-search-button"
                  >
                    <SearchIcon className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Search</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    asChild
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Settings"
                    className="size-6 rounded-sm text-muted-foreground hover:text-foreground"
                  >
                    {/* No onNavClick: on mobile, entering Settings keeps the
                    drawer open and swaps it to the section list. */}
                    <Link to="/settings" data-testid="settings-button">
                      <SettingsIcon className="size-4" />
                    </Link>
                  </Button>
                </TooltipTrigger>
                <TooltipContent side="bottom">Settings</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    aria-label="Close sidebar"
                    onClick={onClose}
                    className="size-6 rounded-sm text-muted-foreground hover:text-foreground"
                  >
                    {/* panel-right-open while the sidebar IS open — this button
                    only renders in the open state (ChatHeader's PanelLeftIcon
                    covers the collapsed state). */}
                    <PanelRightOpenIcon className="size-4" />
                  </Button>
                </TooltipTrigger>
                {/* Bottom placement keeps the tooltip clear of the macOS
                Electron shell's traffic lights at the window's top edge. */}
                <TooltipContent side="bottom">Collapse sidebar</TooltipContent>
              </Tooltip>
            </div>
          </div>

          <div className="flex flex-col gap-0 px-3 py-3" data-testid="sidebar-primary-nav">
            {/* "New session" routes to the home composer ("/"), which now owns
            session creation end-to-end (host/workspace/worktree chips +
            send). Rendered as a Link so cmd/middle-click opens it in a new
            tab; onNavClick still closes the sidebar on a plain mobile tap. */}
            <Button
              asChild
              className={cn(
                // px-2 + gap-1 puts the icon on the sidebar's left (red) column
                // and the label on the label (blue) column — matching section
                // headers and project folders.
                "sidebar-compact-text h-7 w-full justify-start gap-2 rounded-[var(--radius-otto-button)] px-2 font-normal",
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
                onClick={(e) => {
                  setActiveTab("mine");
                  onNavClick(e);
                }}
              >
                <SquarePenIcon className="size-3.5 text-muted-foreground" />
                New session
              </Link>
            </Button>
            {selectionMode ? (
              <BulkActionBar
                selectedIds={selectedIds}
                allConversations={loadedRows}
                visibleCount={visibleConversationCount}
                onSelectAll={() => selectAll(getVisibleConversationsRef.current())}
                onDeselectAll={deselectAll}
                onClear={deselectAll}
                onExit={exitSelectionMode}
              />
            ) : (
              <>
                <Button
                  asChild
                  variant="ghost"
                  className={cn(
                    "sidebar-compact-text h-7 w-full justify-start gap-2 rounded-[var(--radius-otto-button)] border-0 px-2 font-normal",
                    SIDEBAR_HOVER_HIGHLIGHT,
                    isInboxPage && SIDEBAR_ACTIVE_HIGHLIGHT,
                  )}
                  data-testid="inbox-button"
                >
                  <Link to="/inbox" onClick={onNavClick}>
                    <InboxIcon className="size-3.5 text-muted-foreground" />
                    Inbox
                    {inboxCount > 0 && (
                      <span
                        aria-label={
                          inboxCount === 1
                            ? "1 inbox item waiting"
                            : `${inboxCount} inbox items waiting`
                        }
                        className="ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-warning/15 px-1 text-10 font-medium text-warning tabular-nums"
                      >
                        {inboxCount}
                      </span>
                    )}
                  </Link>
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  aria-label="Select sessions"
                  data-testid="toggle-selection-mode"
                  className={cn(
                    "sidebar-compact-text h-7 w-full justify-start gap-2 rounded-[var(--radius-otto-button)] border-0 px-2 font-normal",
                    SIDEBAR_HOVER_HIGHLIGHT,
                  )}
                  onClick={() => setSelectionMode(true)}
                >
                  <ListChecksIcon className="size-3.5 text-muted-foreground" />
                  Select sessions
                </Button>
              </>
            )}
          </div>

          {/* Session-scope tabs: split the viewer's own sessions ("My
          sessions") from ones shared with them ("Shared with me"). Sits above
          the scrolling list (non-scrolling) so it stays put while the list
          scrolls. Hidden during selection mode, where the bulk-action bar owns
          this strip. */}
          {multiUser && !selectionMode && (
            <div className="px-2 pb-2">
              <Tabs
                value={activeTab}
                onValueChange={(v) => setActiveTab(v as SidebarTab)}
                className="w-full"
              >
                <TabsList className="w-full">
                  <TabsTrigger
                    value="mine"
                    data-testid="sidebar-tab-mine"
                    className="sidebar-compact-text min-w-0 font-normal hover:bg-[var(--sidebar-hover)] data-active:bg-[var(--sidebar-active)] data-active:text-[var(--sidebar-active-foreground)] data-active:shadow-none"
                  >
                    <span className="min-w-0 truncate">My sessions</span>
                  </TabsTrigger>
                  <TabsTrigger
                    value="shared"
                    data-testid="sidebar-tab-shared"
                    className="sidebar-compact-text min-w-0 font-normal hover:bg-[var(--sidebar-hover)] data-active:bg-[var(--sidebar-active)] data-active:text-[var(--sidebar-active-foreground)] data-active:shadow-none"
                  >
                    <span className="min-w-0 truncate">Shared with me</span>
                  </TabsTrigger>
                </TabsList>
              </Tabs>
            </div>
          )}

          <nav
            ref={scrollContainerRef}
            className="relative flex-1 overflow-y-auto px-2 pb-3 [scrollbar-gutter:stable]"
          >
            <ConversationList
              conversationsQuery={conversationsQuery}
              scrollContainerRef={scrollContainerRef}
              onRowClick={onNavClick}
              searchQuery=""
              activeTab={multiUser ? activeTab : "mine"}
              pinnedConversationIds={pinnedConversationIds}
              onPinnedConversationIdsChange={setPinnedConversationIds}
              onTogglePinned={togglePinnedConversation}
              selectionMode={selectionMode}
              selectedIds={selectedIds}
              onToggleSelected={toggleSelected}
              getVisibleIdsRef={getVisibleIdsRef}
              getVisibleConversationsRef={getVisibleConversationsRef}
              onVisibleCountChange={setVisibleConversationCount}
            />
          </nav>
        </>
      )}
    </aside>
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
        "flex w-full cursor-pointer items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-muted-foreground text-xs hover:bg-muted disabled:pointer-events-none disabled:opacity-50",
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
  expanded,
  marker,
  onToggleCollapsed,
  pinnedConversationIds,
  activeOverride,
  scrollRoot,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  onProjectAssigned,
  projectRenderedIdsRef,
}: {
  name: string;
  /** First-class project id, or null for a label-only folder. */
  projectId: string | null;
  expanded: boolean;
  marker: SessionState | null;
  onToggleCollapsed: () => void;
  pinnedConversationIds: string[];
  activeOverride: ActiveChatOverride | null;
  scrollRoot: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  onProjectAssigned?: (projectName: string) => void;
  projectRenderedIdsRef?: RefObject<Map<string, string[]>>;
}) {
  const query = useProjectSessions(name, expanded);
  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  const conversations = useMemo(() => {
    const loaded = query.data?.pages.flatMap((page) => page.data) ?? [];
    // Pinned sessions live in the global Pinned section, not their folder.
    return sortByUpdatedAtDesc(
      loaded.filter((c) => !pinnedSet.has(c.id)),
      activeOverride,
    );
  }, [query.data, pinnedSet, activeOverride]);

  // Register this folder's rendered IDs synchronously during render so the
  // shift-select range uses the real per-project data, not the global list.
  const renderedIds = useMemo(
    () => (expanded ? conversations.map((c) => c.id) : []),
    [expanded, conversations],
  );
  if (projectRenderedIdsRef) {
    projectRenderedIdsRef.current.set(name, renderedIds);
  }

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
          expanded ? (
            <FolderOpenIcon className="size-4 shrink-0" />
          ) : (
            <FolderIcon className="size-4 shrink-0" />
          )
        }
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
        emptyMessage={loadingFirstPage ? undefined : "No sessions"}
        indentRows
        headerAction={
          <ProjectFolderActions projectName={name} projectId={projectId} onNavigate={onRowClick} />
        }
        footer={
          loadingFirstPage ? (
            <p className="px-2 py-1 pl-5 text-muted-foreground text-xs">Loading…</p>
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
    </div>
  );
}

interface ConversationListProps {
  conversationsQuery: ReturnType<typeof useConversations>;
  // The scrollable ancestor, used as the infinite-scroll observer root.
  scrollContainerRef: RefObject<HTMLElement | null>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  searchQuery: string;
  activeTab: SidebarTab;
  pinnedConversationIds: string[];
  onPinnedConversationIdsChange: (ids: string[]) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string, shiftKey?: boolean) => void;
  getVisibleIdsRef: RefObject<() => string[]>;
  getVisibleConversationsRef: RefObject<() => Conversation[]>;
  onVisibleCountChange: (count: number) => void;
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
  activeTab,
  pinnedConversationIds,
  onPinnedConversationIdsChange,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
  getVisibleIdsRef,
  getVisibleConversationsRef,
  onVisibleCountChange,
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
  // All loaded conversations from the single paginated list (for pinned
  // backfill, normalization, and the flat session list).
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

  // Each ProjectFolder registers its actually-rendered conversation IDs here
  // (synchronously during render) so shift-select ranges use the real rendered
  // order, not the global paginated list which can diverge.
  const projectRenderedIdsRef = useRef<Map<string, string[]>>(new Map());

  // Backfill pinned sessions that aren't in the loaded set.
  const loadedIds = useMemo(() => new Set(allConversations.map((c) => c.id)), [allConversations]);
  const pinnedBackfill = usePinnedConversationBackfill(pinnedConversationIds, loadedIds);

  // Freeze the active chat's sort key while you're inside it so an
  // updated_at bump from sending a message doesn't reorder the row
  // out from under you. Snapshot is dropped on navigate-away so the
  // chat snaps back to its real position once you've left.
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const [activeOverride, setActiveOverride] = useState<ActiveChatOverride | null>(null);
  useEffect(() => {
    setActiveOverride((prev) => computeNextActiveOverride(activeId, allConversations, prev));
  }, [activeId, allConversations]);

  // Build sections: Pinned and Archived are peeled off; the rest splits into
  // the viewer's own sessions (Chats) and ones shared with them. Archived
  // sessions render in their own group at the bottom (below "Shared with
  // me"); a pinned-then-archived session shows under Archived, not Pinned.
  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  const sections = useMemo(() => {
    // Dedupe by id: the pinned-backfill can return a session already present in
    // the paginated list, and merging both would render the row twice.
    const allWithBackfill = dedupeConversationsById([...allConversations, ...pinnedBackfill]);
    const notArchived = allWithBackfill.filter((c) => c.archived !== true);
    // Each tab shows a disjoint slice — "mine" is the sessions the viewer owns,
    // "shared" is the ones others shared with them. The Pinned / Projects /
    // Sessions structure is then built from that slice, so both tabs reuse the
    // same section layout with different conversations.
    const tabScoped =
      activeTab === "shared"
        ? notArchived.filter((c) => !isOwnedByViewer(c, viewerId))
        : notArchived.filter((c) => isOwnedByViewer(c, viewerId));

    // Pinned takes precedence over Project: pinning a session moves it OUT of
    // its project into the flat global Pinned section (no nested pins). Ordered
    // strictly by when they were pinned (newest pin at the bottom), not by
    // `updated_at`, so a pinned session doesn't jump when it gets a new message.
    // Pins are localStorage and ownership-agnostic, so a pinned shared session
    // floats to Pinned on the Shared tab just like an owned one on My sessions.
    const pinned = orderByPinnedSequence(
      tabScoped.filter((c) => pinnedSet.has(c.id)),
      pinnedConversationIds,
    );
    const pinnedIdSet = new Set(pinned.map((c) => c.id));

    // Projects are a "My sessions"-only tool (filing into a project is
    // owner-only), so the Shared tab renders no folders. On "mine" each folder
    // holds its non-pinned, non-archived sessions; a pinned member is excluded
    // (it lives under Pinned), so pinning a project's last session leaves the
    // folder showing "No sessions".
    const filedIds = new Set<string>();
    const projectGroups: { id: string | null; name: string; conversations: Conversation[] }[] =
      activeTab === "shared"
        ? []
        : projects.map(({ id, name }) => {
            // Dual-read membership: a session belongs to this folder if it has
            // the first-class id OR the legacy omni_project label of this name.
            const inProject = tabScoped.filter(
              (c) =>
                ((id !== null && c.project_id === id) || c.labels?.[PROJECT_LABEL_KEY] === name) &&
                !pinnedIdSet.has(c.id),
            );
            inProject.forEach((c) => filedIds.add(c.id));
            return { id, name, conversations: sortByUpdatedAtDesc(inProject, activeOverride) };
          });
    // NOTE: empty projects are intentionally NOT filtered out. A project comes
    // from the server project list (useProjects), so it can have zero *loaded*
    // conversations — either genuinely empty or because its chats live on an
    // unloaded page. We render it as a folder with a "No sessions" placeholder
    // rather than hiding it (matches the target sidebar layout).

    // Sessions: the remainder of the tab's slice — not pinned, not filed.
    const sessions = sortByUpdatedAtDesc(
      tabScoped.filter((c) => !pinnedIdSet.has(c.id) && !filedIds.has(c.id)),
      activeOverride,
    );
    const archived = sortByUpdatedAtDesc(
      allWithBackfill.filter((c) => c.archived === true),
      activeOverride,
    );
    return { pinned, sessions, archived, projectGroups };
  }, [
    allConversations,
    pinnedBackfill,
    pinnedSet,
    pinnedConversationIds,
    activeOverride,
    projects,
    activeTab,
    viewerId,
  ]);

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
  // True only while the open set was produced by "Expand all" and hasn't been
  // touched since. This — not "do all folders happen to be open" — is what gates
  // the revert affordance, so a user who opens every folder by hand (forced with
  // a single project) never sees a "Revert to last state" button backed by an
  // empty snapshot that would destructively collapse everything.
  const [expandedViaButton, setExpandedViaButton] = useState(false);
  const toggleProjectExpanded = useCallback((projectName: string) => {
    setExpandedViaButton(false);
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
      setExpandedViaButton(false);
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
      | { label?: string; project?: string | null; isPinned?: boolean }
      | undefined;
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

  // "Expand all" opens every project folder at once and remembers the set that
  // was open beforehand, so a follow-up "Revert to last state" restores exactly
  // what was open (not collapse-everything). The snapshot is session-only — not
  // persisted.
  const [revertSnapshot, setRevertSnapshot] = useState<string[]>([]);
  const expandAllProjects = useCallback((allNames: string[]) => {
    setExpandedProjects((prev) => {
      setRevertSnapshot(prev);
      setExpandedViaButton(true);
      writeExpandedProjectSections(allNames);
      return allNames;
    });
  }, []);
  // "Revert to last state" restores the set that was open before "Expand all".
  // When there's no real last state — folders were opened by hand, not via the
  // button (expandedViaButton is false, so any leftover snapshot is stale) — it
  // collapses everything instead.
  const revertProjects = useCallback(() => {
    setExpandedProjects(() => {
      const target = expandedViaButton ? revertSnapshot : [];
      setExpandedViaButton(false);
      writeExpandedProjectSections(target);
      return target;
    });
  }, [expandedViaButton, revertSnapshot]);

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
  useEffect(() => {
    onVisibleCountChange(orderedConversationIds.length);
  }, [orderedConversationIds.length, onVisibleCountChange]);
  getVisibleConversationsRef.current = () => {
    const visible = (title: string, list: readonly Conversation[]) =>
      effectiveCollapsedSections.includes(title) ? [] : [...list];
    const projectsCollapsed = effectiveCollapsedSections.includes("Projects");
    const projectVisible = (name: string, list: readonly Conversation[]) =>
      !projectsCollapsed && expandedProjects.includes(name) ? [...list] : [];
    return [
      ...visible("Pinned", sections.pinned),
      ...sections.projectGroups.flatMap((g) => projectVisible(g.name, g.conversations)),
      ...visible("Chats", sections.sessions),
    ];
  };
  // Getter that builds the shift-select visible order on demand (at click
  // time). Reading projectRenderedIdsRef lazily — rather than snapshotting it
  // during render — guarantees the project segment is always fresh even when a
  // ProjectFolder re-renders independently (async query resolve, session
  // re-sort) without triggering a parent re-render.
  getVisibleIdsRef.current = () => {
    const vis = (title: string, list: readonly Conversation[]) =>
      effectiveCollapsedSections.includes(title) ? [] : list.map((c) => c.id);
    const projCollapsed = effectiveCollapsedSections.includes("Projects");
    return [
      ...vis("Pinned", sections.pinned),
      ...(projCollapsed
        ? []
        : sections.projectGroups.flatMap((g) => projectRenderedIdsRef.current.get(g.name) ?? [])),
      ...vis("Chats", sections.sessions),
    ];
  };
  useSessionSwitchHotkey(orderedConversationIds, activeId);

  // Cmd/Ctrl+1..9/0 jumps to the first ten pinned sessions (desktop only;
  // see the hook). Empty when the Pinned section is collapsed.
  const pinnedSessionIds = useMemo(
    () => (collapsedSections.includes("Pinned") ? [] : sections.pinned.map((c) => c.id)),
    [sections.pinned, collapsedSections],
  );
  usePinnedSessionHotkeys(pinnedSessionIds, activeId);

  // Only normalize pinned ids once all pages are loaded; a pin that
  // lives on an unloaded page should not be dropped prematurely
  // (the backfill covers it in the meantime).
  const hasMorePages = conversationsQuery.hasNextPage;
  const { fetchNextPage, isFetchingNextPage } = conversationsQuery;
  useEffect(() => {
    if (!conversationsQuery.data || hasMorePages || searchQuery) return;
    const allLoaded = dedupeConversationsById([...allConversations, ...pinnedBackfill]);
    const normalized = normalizePinnedConversationIds(pinnedConversationIds, allLoaded);
    if (!sameStringArray(normalized, pinnedConversationIds)) {
      onPinnedConversationIdsChange(normalized);
    }
  }, [
    conversationsQuery.data,
    hasMorePages,
    searchQuery,
    allConversations,
    pinnedBackfill,
    pinnedConversationIds,
    onPinnedConversationIdsChange,
  ]);

  if (conversationsQuery.isLoading) {
    return <p className="px-2 py-1 text-muted-foreground text-xs">Loading…</p>;
  }
  if (conversationsQuery.isError) {
    const err = conversationsQuery.error;
    return (
      <p className="px-2 py-1 text-destructive text-xs">
        Failed to load: {err instanceof Error ? err.message : String(err)}
      </p>
    );
  }
  const showShared = activeTab === "shared";
  const emptyMessage = searchQuery
    ? "No matching conversations"
    : showShared
      ? "No sessions shared with you"
      : "No active sessions";

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
        <div className="flex flex-col gap-4 pr-1">
          {/* Removing a filed session from its project means dropping it back
            onto the flat "Chats" list — so the Chats section itself is the
            ungroup target (wrapped below). This top strip is only a FALLBACK
            for when there are no ungrouped chats yet, so the Chats section
            isn't rendered and there'd otherwise be nowhere to drop. */}
          {!showShared && activeDrag?.project != null && sections.sessions.length === 0 && (
            <UngroupDropZone />
          )}
          {totalVisible === 0 ? (
            <>
              <p className="px-2 py-1 text-muted-foreground text-xs">{emptyMessage}</p>
              {/* The list is one paginated stream ordered by updated_at across
              owned + shared sessions, so the current tab can be empty on the
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
                    selectionMode={selectionMode}
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
              Shown on the "mine" tab even with zero projects, so "New project"
              (create-empty) is discoverable — projects are a My-sessions tool. */}
              {activeTab !== "shared" && (
                <SectionGroup
                  title="Projects"
                  collapsed={effectiveCollapsedSections.includes("Projects")}
                  onToggleCollapsed={() => effectiveToggleSectionCollapsed("Projects")}
                  headerAction={(() => {
                    const allNames = sections.projectGroups.map((g) => g.name);
                    // "New project" is always available (even when collapsed) so
                    // the create-empty flow is reachable; the expand/revert control
                    // is only meaningful when there are folders and the group is open.
                    const showExpandControls =
                      !effectiveCollapsedSections.includes("Projects") && allNames.length > 0;
                    // Once every folder is open the only useful move is to undo it,
                    // so the control flips to "revert" — which restores the set open
                    // before "Expand all", or collapses everything when there's no
                    // real last state (folders opened by hand). Otherwise it expands.
                    const allExpanded =
                      allNames.length > 0 && allNames.every((n) => expandedProjects.includes(n));
                    return (
                      <div className="flex items-center">
                        {showExpandControls &&
                          (allExpanded ? (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label="Collapse to previous"
                                  data-testid="revert-projects"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    revertProjects();
                                  }}
                                >
                                  <Minimize2Icon className="size-3.5" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent side="bottom">Collapse to previous</TooltipContent>
                            </Tooltip>
                          ) : (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon-sm"
                                  aria-label="Expand all"
                                  data-testid="expand-all-projects"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    expandAllProjects(allNames);
                                  }}
                                >
                                  <Maximize2Icon className="size-3.5" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent side="bottom">Expand all</TooltipContent>
                            </Tooltip>
                          ))}
                        <NewProjectButton onCreated={expandProject} />
                      </div>
                    );
                  })()}
                >
                  {sections.projectGroups.map((group) => (
                    <ProjectFolder
                      key={group.name}
                      name={group.name}
                      projectId={group.id}
                      expanded={expandedProjects.includes(group.name)}
                      // Best-effort marker from the globally-loaded window: a
                      // collapsed folder hasn't fetched its own sessions yet.
                      marker={projectMarkerState(group.conversations)}
                      onToggleCollapsed={() => toggleProjectExpanded(group.name)}
                      pinnedConversationIds={pinnedConversationIds}
                      activeOverride={activeOverride}
                      scrollRoot={scrollContainerRef}
                      onRowClick={onRowClick}
                      onTogglePinned={onTogglePinned}
                      selectionMode={selectionMode}
                      selectedIds={selectedIds}
                      onToggleSelected={onToggleSelected}
                      onProjectAssigned={expandProject}
                      projectRenderedIdsRef={projectRenderedIdsRef}
                    />
                  ))}
                  {sections.projectGroups.length === 0 &&
                    !effectiveCollapsedSections.includes("Projects") && (
                      <p className="px-3 py-1.5 text-xs text-muted-foreground">
                        No projects yet. Create one to group your sessions.
                      </p>
                    )}
                </SectionGroup>
              )}
              {sections.sessions.length > 0 && (
                // Drop a session here to send it to the flat "Chats" list — where
                // unfiled, unpinned sessions live. Active while dragging a filed
                // session (removes it from its project) or a pinned one (unpins
                // it), since both have somewhere to land here.
                <ChatsDropZone
                  active={activeDrag != null && (activeDrag.project != null || activeDrag.isPinned)}
                >
                  <ConversationSection
                    title="Sessions"
                    conversations={sections.sessions}
                    pinnedConversationIds={pinnedConversationIds}
                    collapsed={effectiveCollapsedSections.includes("Chats")}
                    onToggleCollapsed={() => effectiveToggleSectionCollapsed("Chats")}
                    onRowClick={onRowClick}
                    onTogglePinned={onTogglePinned}
                    selectionMode={selectionMode}
                    selectedIds={selectedIds}
                    onToggleSelected={onToggleSelected}
                    onProjectAssigned={expandProject}
                  />
                </ChatsDropZone>
              )}
              {/* Both tabs render this same Pinned / Projects / Sessions tree;
              `sections` is scoped to the active tab's conversations (owned vs.
              shared), and Projects is empty on the Shared tab. */}
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
        {/* The dragged row's preview follows the pointer (rendered in a portal),
          a compact card showing the session's title. */}
        <DragOverlay dropAnimation={null}>
          {activeDrag ? (
            <div className="pointer-events-none max-w-[16rem] truncate rounded-md border bg-card-solid px-3 py-2 text-sm shadow-lg">
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
        "flex items-center gap-1.5 rounded-md border border-dashed border-border px-2 py-1.5 text-muted-foreground text-xs transition-colors",
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
// group, so they all align and animate identically (icon · title · marker ·
// hover-chevron). Headers carry no count badge.
function SectionHeader({
  title,
  icon,
  marker,
  hasAction,
  collapsed,
  onToggleCollapsed,
}: {
  title: string;
  icon?: ReactNode;
  marker?: SessionState | null;
  /** Whether the section also renders a hover-revealed header action (the
      project-folder kebab), which shares the header's right edge with the
      collapsed marker. */
  hasAction?: boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  return (
    <h2>
      <button
        type="button"
        aria-expanded={!collapsed}
        onClick={onToggleCollapsed}
        className={
          icon
            ? `${cn(
                "group flex w-full items-center gap-2 rounded-[var(--radius-otto-button)] border-0 px-2 py-[3px] text-left transition-colors",
                SIDEBAR_HOVER_HIGHLIGHT,
              )} sidebar-compact-text text-foreground`
            : "group flex w-full items-center gap-1 border-0 pt-0 pr-0 pb-1 pl-2 text-left text-xs leading-4 text-muted-foreground transition-colors hover:text-foreground"
        }
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
        {collapsed && marker && (
          <span
            className={cn(
              "ml-auto flex shrink-0 items-center transition-opacity",
              // When the header also carries a hover-revealed kebab, keep the
              // marker clear of it the same way a row's time/marker slot does:
              // reserve space on mobile (kebab always shown) and fade out on
              // desktop hover so the kebab takes its place.
              hasAction &&
                "mr-14 md:mr-0 md:group-hover/section:opacity-0 md:group-focus-within/section:opacity-0",
            )}
          >
            <SessionStateBadge state={marker} />
          </span>
        )}
      </button>
    </h2>
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
  children,
}: {
  title: string;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Optional control overlaid at the group header's right edge (e.g. the
      "collapse all projects" toggle). Hover/focus-revealed on desktop. */
  headerAction?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      <div className="group/header relative">
        <SectionHeader
          title={title}
          hasAction={headerAction != null}
          collapsed={collapsed}
          onToggleCollapsed={onToggleCollapsed}
        />
        {headerAction && (
          // Desktop-only, hover/keyboard-focus-revealed: a group-level bulk
          // control (e.g. "expand all projects") is a pointer convenience, so it
          // stays hidden until the header is hovered and never floats on touch
          // viewports where there's no hover. Reveal on :focus-visible (keyboard)
          // — NOT :focus-within — so clicking the button with the mouse doesn't
          // leave it stuck visible: React reuses the same node when it swaps
          // expand↔revert, so the clicked button keeps focus afterward.
          <div className="absolute top-0.5 right-1 hidden items-center transition-opacity md:flex md:opacity-0 md:has-[:focus-visible]:opacity-100 md:group-hover/header:opacity-100">
            {headerAction}
          </div>
        )}
      </div>
      {!collapsed && <div className="flex flex-col gap-0.5">{children}</div>}
    </section>
  );
}

function ConversationSection({
  title,
  icon,
  marker,
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
  footer,
  onProjectAssigned,
}: {
  title?: string;
  /** Optional icon rendered before the title (e.g. project folder icon). */
  icon?: ReactNode;
  /** When collapsed, the aggregate marker of hidden rows (same badge as a row). */
  marker?: SessionState | null;
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
  emptyMessage?: string;
  /** Indent the rows one extra step (used to nest a project's chats). */
  indentRows?: boolean;
  /** Optional control overlaid at the header's right edge (e.g. a project's
      kebab). Hover/focus-revealed on desktop, always shown on mobile. */
  headerAction?: ReactNode;
  /** Optional content rendered after the rows inside the expanded body (e.g. a
      project folder's own infinite-scroll sentinel / loading row). */
  footer?: ReactNode;
  /** Called with the project name when a row is filed into one, so the sidebar
      can expand that (possibly brand-new) project folder. */
  onProjectAssigned?: (projectName: string) => void;
}) {
  // An untitled section is always open — there's no header to collapse it.
  const isCollapsed = title != null && collapsed;
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
            hasAction={headerAction != null}
            collapsed={isCollapsed}
            onToggleCollapsed={onToggleCollapsed}
          />
          {headerAction && (
            <div className="absolute top-0.5 right-1 flex items-center transition-opacity md:opacity-0 md:group-focus-within/header:opacity-100 md:group-hover/header:opacity-100 md:group-has-[[data-state=open]]/header:opacity-100">
              {headerAction}
            </div>
          )}
        </div>
      )}
      {!isCollapsed && (
        <>
          {conversations.length === 0 && emptyMessage ? (
            // Expanded but empty (e.g. a project with no loaded chats).
            <p className={cn("px-2 py-1 text-muted-foreground text-xs", indentRows && "pl-5")}>
              {emptyMessage}
            </p>
          ) : (
            // Indent project chats a step under the project-folder name above.
            <ul className={cn("flex flex-col", indentRows ? "gap-0 pl-6" : "gap-0.5")}>
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
type MenuItemProps = {
  children?: ReactNode;
  className?: string;
  disabled?: boolean;
  variant?: "default" | "destructive";
  // Radix's menu `onSelect` receives a native Event in both families.
  onSelect?: (event: Event) => void;
  "data-testid"?: string;
};

type MenuComponents = {
  Item: ComponentType<MenuItemProps>;
  Separator: ComponentType<{ className?: string }>;
  Sub: ComponentType<{ children?: ReactNode }>;
  SubTrigger: ComponentType<{
    children?: ReactNode;
    className?: string;
    "data-testid"?: string;
  }>;
  SubContent: ComponentType<{ children?: ReactNode; className?: string }>;
};

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
        <C.Item data-testid="rename-conversation" onSelect={() => setIsEditing(true)}>
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
            <C.SubContent className="w-56 p-1 [&_[role=menuitem]]:text-xs">
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
      {isOwner ? (
        <C.Item
          data-testid="delete-conversation"
          variant="destructive"
          onSelect={() => setDeleteOpen(true)}
        >
          <Trash2Icon className="size-3.5" />
          Delete
        </C.Item>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <div>
              <C.Item data-testid="delete-conversation" disabled>
                <Trash2Icon className="size-3.5" />
                Delete
              </C.Item>
            </div>
          </TooltipTrigger>
          <TooltipContent side="left">
            Only the session owner can delete this session
          </TooltipContent>
        </Tooltip>
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
      sideOffset={12}
      data-testid="session-tooltip-content"
      className="w-72 max-w-[calc(100vw-2rem)] flex-col items-stretch gap-3 rounded-xl bg-card-solid px-4 py-3 whitespace-normal shadow-lg"
    >
      <div className="text-sm font-medium leading-snug">
        {conversation.title ?? conversation.id}
        <span className="font-normal text-muted-foreground">
          {" · "}
          {relativeTime(conversation.updated_at * 1000)}
        </span>
      </div>
      <div
        data-testid="session-tooltip-location"
        className="flex items-center gap-2 text-sm text-muted-foreground"
      >
        <LaptopIcon aria-hidden className="size-4 shrink-0" />
        <span className="truncate">{locationLabel}</span>
      </div>
    </TooltipContent>
  );
}

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
  // Track the *live* active conversation id. Delete is fire-and-forget,
  // so the user can navigate to another conversation before the mutation
  // resolves — the onSuccess redirect must key off where they are now,
  // not the `isActive` captured when delete was initiated.
  const activeIdRef = useRef(activeId);
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);
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
  const moveToProject = useMoveToProject();
  // Archive stops the runner first (resource hygiene): a hidden session
  // shouldn't keep a runner alive. This is NOT the user-facing Stop action
  // (the kebab's "Stop session" item below, backed by its own mutation) —
  // it's an internal step of archiving. Unarchive + a message relaunches
  // on the live host under the non-sticky-stop model.
  const stopForArchive = useStopSession();
  // The kebab's user-facing "Stop session" action — separate mutation
  // instance so its pending/error state can't bleed into archiving's.
  const stopSession = useStopSession();
  const isArchived = conversation.archived === true;
  const [isEditing, setIsEditing] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [stopOpen, setStopOpen] = useState(false);
  // The kebab menu is controlled so the project submenu can close the whole
  // menu after a pick (a plain click inside the submenu wouldn't otherwise).
  const [menuOpen, setMenuOpen] = useState(false);
  // Opt-in "delete local branch" checkbox (worktree sessions only).
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  // True while an archive is in flight. Drives the "Archiving…" status
  // row, mirroring delete's "Deleting…" indicator — without it the row
  // shows nothing while the archive completes.
  const [isArchiving, setIsArchiving] = useState(false);
  const gitBranch = conversation.git_branch ?? null;
  // Every row action gates on ownership alone — the sidebar carries no
  // effective-permission level, so rename/share/move/drag are owner-only and
  // non-owners get a read-only row. (Finer-grained edit/manage affordances
  // live on the open-session view, which fetches the caller's real level.)
  const isOwner = isOwnedByViewer(conversation, useViewerId());
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

  const label = conversationDisplayLabel(conversation);
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
  const sessionState =
    derivedState?.kind === "awaiting"
      ? derivedState
      : hasUnseenMessages
        ? { kind: "unseen" as const }
        : derivedState;

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
    if (was && !isDragging) {
      justDraggedRef.current = true;
      const timer = setTimeout(() => {
        justDraggedRef.current = false;
      }, 0);
      return () => clearTimeout(timer);
    }
  }, [isDragging]);
  // Merge the drag node ref with the row ref used for scroll-into-view.
  const setRowRef = useCallback(
    (node: HTMLLIElement | null) => {
      rowRef.current = node;
      setDragNodeRef(node);
    },
    [setDragNodeRef],
  );

  if (isEditing) {
    return (
      <li>
        <ConversationEditRow
          initialTitle={conversation.title ?? ""}
          onCommit={(title) => {
            // Bail on no-op edits so we don't fire an unnecessary PATCH.
            const trimmed = title.trim();
            if (trimmed && trimmed !== (conversation.title ?? "")) {
              rename.mutate({ id: conversation.id, title: trimmed });
            }
            setIsEditing(false);
          }}
          onCancel={() => setIsEditing(false)}
        />
      </li>
    );
  }

  // While a delete is in flight (or after it failed), swap the
  // interactive row for a status row so the user sees progress without
  // the dialog blocking. On success the row is spliced out of the
  // cached list and this row unmounts; on error we keep it with
  // retry/dismiss affordances.
  if (del.isPending || del.isError) {
    return (
      <li>
        <DeletingRow
          label={label}
          isError={del.isError}
          // `del.variables` holds the args from the last mutate call,
          // so retry replays the exact same delete (incl. deleteBranch).
          onRetry={() => del.variables && runDelete(del.variables)}
          onDismiss={() => del.reset()}
        />
      </li>
    );
  }

  // Archiving runs stop→archive (see runArchive); show a status row for
  // the whole span instead of leaving the row looking idle. On success
  // the list refetches and the row drops out of the default view (or
  // flips to its archived state under "Show archived"); on failure the
  // flag clears and the interactive row returns so the user can retry.
  if (isArchiving) {
    return (
      <li>
        <ArchivingRow label={label} />
      </li>
    );
  }

  function runDelete(args: { id: string; deleteBranch?: boolean }) {
    del.mutate(args, {
      onSuccess: () => {
        // If the user is *still* viewing the conversation we just
        // deleted, bounce back to `/` so the chat surface doesn't
        // 404-loop on the now-missing id. Read the live activeId (ref)
        // — they may have navigated away while the delete was in flight.
        if (activeIdRef.current === conversation.id) navigate("/", { replace: true });
      },
    });
  }

  function confirmDelete() {
    // Fire-and-forget: close the dialog immediately so the user isn't
    // blocked on the (potentially slow) DELETE — worktree cleanup can
    // take seconds. The row renders its own "Deleting…" indicator while
    // `del.isPending`, and a retryable error state if it fails.
    const args = { id: conversation.id, deleteBranch: gitBranch !== null && deleteBranch };
    setDeleteOpen(false);
    setDeleteBranch(false);
    runDelete(args);
  }

  function runArchive() {
    const nextArchived = !isArchived;
    // Unarchiving is a quick flag flip — no status row.
    if (!nextArchived) {
      archive.mutate({ id: conversation.id, archived: false });
      return;
    }
    // Archiving runs stop→archive: stop the runner first (best-effort) so a
    // hidden session doesn't leave a runner orphaned, then flip the flag.
    // Show "Archiving…" for the whole span; cleared on the archive's settle
    // (success → row leaves the default list or shows archived; failure →
    // interactive row returns for a retry). The stop is best-effort — an
    // already-offline / wedged runner must not block the archive.
    setIsArchiving(true);
    stopForArchive.mutate(conversation.id, {
      onSettled: () => {
        archive.mutate(
          { id: conversation.id, archived: true },
          {
            // Point the user at where the session went — it's no longer in
            // the sidebar list, so surface its new home in Settings.
            onSuccess: showArchivedToast,
            onSettled: () => setIsArchiving(false),
          },
        );
      },
    });
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
    runArchive,
  };

  // The clickable row surface. Extracted so it can be rendered bare (selection
  // mode) or wrapped in the right-click ContextMenuTrigger below.
  const rowLink = (
    <Link
      to={selectionMode ? "#" : `/c/${conversation.id}`}
      className={cn(
        "sidebar-compact-text relative flex min-h-7 flex-col gap-0.5 rounded-[var(--radius-otto-sm)] px-2 py-0.5 text-left text-foreground transition-[color,background-color,transform] duration-[var(--duration-otto-fast)] ease-[var(--ease-otto)] motion-safe:hover:-translate-y-px",
        SIDEBAR_HOVER_HIGHLIGHT,
        selectionMode ? "w-full" : "w-[calc(100%+1rem)]",
        !selectionMode && (sessionState?.kind === "awaiting" ? "pr-48 md:pr-29" : "pr-28 md:pr-14"),
        selectionMode && "pr-10",
        isActive && SIDEBAR_ACTIVE_HIGHLIGHT,
        selectionMode && isSelected && SIDEBAR_ACTIVE_HIGHLIGHT,
      )}
      onClick={(e) => {
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
        setIsEditing(true);
      }}
      title={isMobile ? (conversation.title ?? conversation.id) : undefined}
    >
      {/* Row 1: the session name. Status markers (working, needs-approval,
          unseen) render in the trailing session-state slot below, not inline
          here. Leading icons (agent type, pin, shared) were removed to keep
          rows text-clean; pinned rows still group under "Pinned". */}
      <div className="flex w-full items-center gap-1.5">
        <span className="relative min-w-0 truncate">
          {label}
          {hasUnseenMessages && <span className="sr-only"> (unread)</span>}
        </span>
      </div>
      {/* Row 2: git branch subtitle, spanning the full row below. */}
      {gitBranch !== null && (
        <span
          className="flex items-center gap-1 font-normal text-xs text-muted-foreground"
          title={gitBranch}
        >
          <GitBranchIcon className="size-3 shrink-0" />
          <span className="truncate">{gitBranch}</span>
        </span>
      )}
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
            <ContextMenuContent className="min-w-44 [&_[role=menuitem]]:text-xs">
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
          />
        </HoverCard>
      ) : isMobile ? (
        <ContextMenu>
          <ContextMenuTrigger asChild>{rowLink}</ContextMenuTrigger>
          <ContextMenuContent className="min-w-44 [&_[role=menuitem]]:text-xs">
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
            <ContextMenuContent className="min-w-44 [&_[role=menuitem]]:text-xs">
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
        <span className="-translate-y-1/2 pointer-events-none absolute top-1/2 right-2.5 flex items-center">
          {isSelected ? (
            <SquareCheckIcon className="size-4 text-primary" />
          ) : (
            <SquareIcon className="size-4 text-muted-foreground" />
          )}
        </span>
      ) : sessionState !== null ? (
        <span className={SESSION_STATE_SLOT_CLASS}>
          <SessionStateBadge state={sessionState} />
        </span>
      ) : null}
      {/* Archived rows omit the pin entirely: pinning is meaningless there
          (archive outranks pin), so there's no pin action even on hover. Also
          hidden while selecting (bulk mode owns the row controls). */}
      {!selectionMode && !isArchived && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={isPinned ? "Unpin conversation" : "Pin conversation"}
          data-testid="quick-pin-conversation"
          className={cn(
            "-translate-y-1/2 absolute top-1/2 right-[14px] transition-opacity",
            // Desktop-only quick affordance: hidden on mobile (the kebab's
            // Pin item below covers that), hover/focus-revealed from `md` up.
            // Pinned rows no longer keep a persistent pin marker, since the
            // "Pinned" section header (and pinned-first ordering inside a
            // project) already conveys the pinned state. Revealed glyph:
            // unpin if pinned, pin otherwise.
            //
            // `md:inline-flex` (not `md:block`): the Button base is
            // `inline-flex` and relies on it for `items-center justify-center`
            // to center the icon. `md:block` would override that display and
            // collapse the centering, leaving the glyph pinned to the top-left
            // of the button — so keep the flex display when revealing it.
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
          {isPinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
        </Button>
      )}
      {!selectionMode && (
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label="Conversation actions"
              data-testid="conversation-actions"
              // Absolute-positioned trigger. On mobile (no hover state)
              // it's always visible. On desktop it stays hidden until
              // hover / keyboard focus, with `aria-expanded` keeping it
              // surfaced while the menu is open so the trigger doesn't
              // vanish under the cursor.
              className={cn(
                "-translate-y-1/2 absolute top-1/2 -right-3 transition-opacity",
                "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
                "md:aria-expanded:opacity-100",
              )}
              onClick={(e) => {
                // Keep the trigger click from bubbling into the Link.
                e.preventDefault();
                e.stopPropagation();
              }}
            >
              <MoreHorizontalIcon className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
          {/* text-xs on every menu item (incl. the submenu trigger): a smaller,
              denser kebab that reads closer to the row text. Scoped here so the
              shared dropdown-menu component is untouched. */}
          <DropdownMenuContent align="end" className="min-w-44 [&_[role=menuitem]]:text-xs">
            <ConversationMenuItems
              components={dropdownBundle}
              setMenuOpen={setMenuOpen}
              {...menuItemProps}
            />
          </DropdownMenuContent>
        </DropdownMenu>
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
              <p className="text-xs text-muted-foreground">
                Optionally clean up the git worktree. These actions are{" "}
                <span className="font-semibold text-destructive">irreversible</span>.
              </p>
              <label className="flex cursor-pointer items-start gap-2 text-sm">
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
                  <code className="break-all rounded bg-muted px-1 py-0.5 text-xs">
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
            >
              Delete
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
            <p className="text-sm text-destructive" role="alert">
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
              onClick={() =>
                stopSession.mutate(conversation.id, { onSuccess: () => setStopOpen(false) })
              }
              disabled={stopSession.isPending}
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
 * row surfaces it again: the session title, then a folder icon + project name.
 * Mirrors {@link AgentHoverCard}'s Cursor-style placement (right / top-aligned)
 * and the muted, small-icon foreground used elsewhere in the sidebar.
 */
function PinnedProjectFlyoutContent({
  title,
  projectName,
}: {
  title: string;
  projectName: string;
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
      <p className="line-clamp-3 font-medium text-sm">{title}</p>
      <p className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
        <FolderIcon className="size-3.5 shrink-0" />
        <span className="truncate">{projectName}</span>
      </p>
    </HoverCardContent>
  );
}

/**
 * Status row shown in place of a conversation while its delete is in
 * flight (`isError === false`) or after it failed (`isError === true`).
 * Keeps the user un-blocked: the delete dialog closes immediately and
 * this surfaces progress / failure inline in the sidebar.
 */
function DeletingRow({
  label,
  isError,
  onRetry,
  onDismiss,
}: {
  label: string;
  isError: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}) {
  if (isError) {
    return (
      <div
        className="flex w-full items-center gap-1.5 rounded-md px-2 py-2 text-sm"
        data-testid="conversation-delete-failed"
        role="alert"
      >
        <AlertTriangleIcon className="size-3.5 shrink-0 text-destructive" />
        {/* Name the session in the visible text — with multiple failed
            deletes the user must be able to tell the rows apart. */}
        <span
          className="min-w-0 flex-1 truncate text-destructive"
          title={`Couldn't delete ${label}`}
        >
          Couldn't delete <span className="font-medium">{label}</span>
        </span>
        <Button type="button" variant="ghost" size="sm" className="h-6 px-1.5" onClick={onRetry}>
          Retry
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Dismiss delete error"
          onClick={onDismiss}
        >
          <XIcon className="size-3.5" />
        </Button>
      </div>
    );
  }
  return (
    <div
      className="flex w-full items-center gap-1.5 rounded-md px-2 py-2 text-sm text-muted-foreground opacity-70"
      data-testid="conversation-deleting"
      aria-live="polite"
    >
      <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
      <span className="min-w-0 flex-1 truncate" title={label}>
        {label}
      </span>
      <span className="shrink-0 text-xs">Deleting…</span>
    </div>
  );
}

/**
 * In-flight status row shown while a session is being archived (the
 * stop→archive sequence in ConversationRow.runArchive). Mirrors the
 * non-error arm of {@link DeletingRow}; archive failures fall back to
 * the interactive row rather than a persistent error state, so there's
 * no retry/dismiss affordance here.
 */
function ArchivingRow({ label }: { label: string }) {
  return (
    <div
      className="flex w-full items-center gap-1.5 rounded-md px-2 py-2 text-sm text-muted-foreground opacity-70"
      data-testid="conversation-archiving"
      aria-live="polite"
    >
      <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
      <span className="min-w-0 flex-1 truncate" title={label}>
        {label}
      </span>
      <span className="shrink-0 text-xs">Archiving…</span>
    </div>
  );
}

// ── ProjectFolderActions ──────────────────────────────────────────────────────

/**
 * The hover-revealed controls on a project-folder header: a kebab menu and a
 * pencil that starts a new session pre-filed under this project. The pencil
 * links to the landing composer with `?project=<name>` so its project chip
 * lands already selected.
 */
function ProjectFolderActions({
  projectName,
  projectId,
  onNavigate,
}: {
  projectName: string;
  /** First-class project id, or null for a label-only folder. */
  projectId: string | null;
  /** Plain-left-click nav handler — closes the mobile overlay so the
      pre-filed new-session page isn't left hidden behind the sidebar. */
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  return (
    <div className="flex items-center">
      {/* Desktop-only quick affordance; on mobile it folds into the kebab's
          "New session" item below. */}
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            asChild
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`New session in ${projectName}`}
            data-testid="project-new-session"
            className="max-md:hidden"
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
              <SquarePenIcon className="size-3.5" />
            </Link>
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">New session in project</TooltipContent>
      </Tooltip>
      <ProjectFolderMenu projectName={projectName} projectId={projectId} onNavigate={onNavigate} />
    </div>
  );
}

// ── ProjectFolderMenu ─────────────────────────────────────────────────────────

/**
 * The kebab on a project-folder header: "Rename project" (O(1) via
 * `PATCH /v1/projects/{id}` for a first-class project; promotes a label-only
 * folder on demand) and "Delete project" (archives + unfiles all members, then
 * removes the container). Delete is confirmed since it archives sessions.
 */
function ProjectFolderMenu({
  projectName,
  projectId,
  onNavigate,
}: {
  projectName: string;
  projectId: string | null;
  /** Nav handler for the mobile-only "New session" item (desktop uses the
      hover-revealed pencil). Closes the sidebar overlay on mobile. */
  onNavigate: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState(projectName);
  const deleteProject = useDeleteProject();
  const renameProject = useRenameProject();

  return (
    <>
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Project actions for ${projectName}`}
            data-testid="project-actions"
            // Sits on the folder header; keep its click off the collapse toggle.
            onClick={(e) => e.stopPropagation()}
          >
            <MoreHorizontalIcon className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-40 [&_[role=menuitem]]:text-xs">
          {/* New session — mobile-only (md:hidden); desktop uses the
              hover-revealed pencil on the folder header. */}
          <DropdownMenuItem asChild className="md:hidden" data-testid="project-new-session-menu">
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
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="rename-project"
            onSelect={() => {
              setRenameValue(projectName);
              setRenameOpen(true);
            }}
          >
            <PencilIcon className="size-3.5" />
            Rename project
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="delete-project"
            variant="destructive"
            onSelect={() => setDeleteOpen(true)}
          >
            <Trash2Icon className="size-3.5" />
            Delete project
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>Rename project</DialogTitle>
          </DialogHeader>
          {/* A <form> so Enter in the input submits natively (Radix Dialog
              doesn't wrap children in one) instead of relying on a manual
              key handler + button lookup. */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const newName = renameValue.trim();
              if (newName === "" || newName === projectName) {
                setRenameOpen(false);
                setMenuOpen(false);
                return;
              }
              renameProject.mutate(
                { id: projectId, oldName: projectName, newName },
                {
                  onSuccess: () => {
                    setRenameOpen(false);
                    setMenuOpen(false);
                  },
                },
              );
            }}
          >
            <input
              autoFocus
              className="w-full rounded-md border bg-transparent px-3 py-2 text-sm outline-none"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
            />
            {renameProject.isError && (
              <p className="text-sm text-destructive" role="alert">
                {(renameProject.error as Error).message}
              </p>
            )}
            <DialogFooter className="border-t-0 bg-transparent">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setRenameOpen(false)}
                disabled={renameProject.isPending}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                data-testid="rename-project-confirm"
                disabled={renameProject.isPending || renameValue.trim() === ""}
              >
                {renameProject.isPending ? "Renaming…" : "Rename"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
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
            <p className="text-sm text-destructive" role="alert">
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
              disabled={deleteProject.isPending}
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
              {deleteProject.isPending ? "Deleting…" : "Delete project"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
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
  const [creatingNew, setCreatingNew] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const newInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (creatingNew) {
      newInputRef.current?.focus();
    }
  }, [creatingNew]);

  const filtered = search
    ? projects.filter((p) => p.name.toLowerCase().includes(search.toLowerCase()))
    : projects;

  function handleNewProjectCommit() {
    const name = newProjectName.trim();
    setCreatingNew(false);
    setNewProjectName("");
    if (name) onSelect(name);
  }

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
          className="w-full bg-transparent text-xs outline-none placeholder:text-muted-foreground"
          placeholder="Search projects"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={swallowKeys}
        />
      </div>
      <div className="max-h-48 overflow-y-auto">
        {filtered.map((p) => (
          <C.Item key={p.name} className="px-2 py-1" onSelect={() => onSelect(p.name)}>
            <span className="flex-1 truncate text-left">{p.name}</span>
            {currentProject === p.name && (
              <CheckMarkIcon className="size-3.5 shrink-0 text-primary" />
            )}
          </C.Item>
        ))}
        {filtered.length === 0 && !creatingNew && (
          <p className="px-2 py-1.5 text-xs text-muted-foreground">No projects yet.</p>
        )}
      </div>
      <div className="border-t pt-1">
        {creatingNew ? (
          <div className="flex items-center gap-1 px-2 py-1">
            <input
              ref={newInputRef}
              className="flex-1 bg-transparent text-xs outline-none"
              placeholder="Project name…"
              value={newProjectName}
              onChange={(e) => setNewProjectName(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") {
                  e.preventDefault();
                  handleNewProjectCommit();
                }
                if (e.key === "Escape") {
                  setCreatingNew(false);
                  setNewProjectName("");
                }
              }}
            />
          </div>
        ) : (
          <C.Item
            className="px-2 py-1"
            // Keep the menu open so the inline input can take over in place.
            onSelect={(e) => {
              e.preventDefault();
              setCreatingNew(true);
            }}
          >
            <PlusIcon className="size-3.5 shrink-0" />
            Create new project
          </C.Item>
        )}
        {currentProject && (
          <C.Item className="px-2 py-1" onSelect={() => onSelect("")}>
            Remove from{" "}
            <span className="rounded bg-muted px-1 py-0.5 font-mono text-[0.95em]">
              {currentProject}
            </span>
          </C.Item>
        )}
      </div>
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
    // pl-1 + the input's px-1 line the text up with the row's px-2 title;
    // py-1 around the size-7 buttons matches the 36px single-line row height.
    <div className="flex items-center gap-1 rounded-md bg-muted py-1 pr-1 pl-1">
      <input
        ref={inputRef}
        type="text"
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
        className="min-w-0 flex-1 truncate rounded bg-transparent px-1 py-1 text-sm outline-none md:select-text"
      />
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
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
        size="icon-sm"
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
  visibleCount,
  onSelectAll,
  onDeselectAll,
  onClear,
  onExit,
}: {
  selectedIds: Set<string>;
  allConversations: Conversation[];
  visibleCount: number;
  onSelectAll: () => void;
  onDeselectAll: () => void;
  onClear: () => void;
  onExit: () => void;
}) {
  const navigate = useNavigate();
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const bulkArchive = useBulkArchiveConversations();
  const bulkDelete = useBulkDeleteConversations();
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
  const allSelected = count > 0 && count === visibleCount;
  const isBusy = bulkArchive.isPending || bulkDelete.isPending;

  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  function handleArchive() {
    if (nonArchivedSelected.length === 0) return;
    bulkArchive.mutate(
      { ids: nonArchivedSelected.map((c) => c.id), archived: true },
      {
        onSuccess: () => {
          onDeselectAll();
        },
      },
    );
  }

  function handleUnarchive() {
    if (archivedSelected.length === 0) return;
    bulkArchive.mutate(
      { ids: archivedSelected.map((c) => c.id), archived: false },
      {
        onSuccess: () => {
          onDeselectAll();
        },
      },
    );
  }

  function handleDelete() {
    const ids = ownedSelected.map((c) => c.id);
    if (ids.length === 0) return;
    setConfirmDeleteOpen(false);
    bulkDelete.mutate(ids, {
      onSuccess: () => {
        if (activeId && ids.includes(activeId)) navigate("/", { replace: true });
        onDeselectAll();
      },
      onError: (err: any) => {
        if (activeId && err?.succeeded?.includes(activeId)) navigate("/", { replace: true });
      },
    });
  }

  return (
    <>
      <div className="relative mt-3 flex flex-col gap-1.5">
        <div className="relative flex min-h-8 items-center gap-1.5 px-2 pr-9">
          <span className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
            {count === 0 ? "None selected" : `${count} selected`}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-1.5 text-sm"
            onClick={allSelected ? onDeselectAll : onSelectAll}
          >
            {allSelected ? "Deselect all" : "Select all"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6 px-1.5 text-sm"
            disabled={count === 0}
            onClick={onClear}
          >
            Clear
          </Button>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="secondary"
                size="icon-sm"
                className="-translate-y-1/2 absolute top-1/2 right-0 shrink-0 rounded-full"
                aria-label="Exit selection mode"
                data-testid="toggle-selection-mode"
                onClick={onExit}
              >
                <XIcon className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Exit selection</TooltipContent>
          </Tooltip>
        </div>

        <div className="flex items-center gap-1.5 px-2">
          {allSelectedSameArchiveGroup && nonArchivedSelected.length > 0 && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 gap-1.5 text-xs"
              disabled={isBusy}
              onClick={handleArchive}
              data-testid="bulk-archive"
            >
              {bulkArchive.isPending ? (
                <Loader2Icon className="size-3 animate-spin" />
              ) : (
                <ArchiveIcon className="size-3" />
              )}
              Archive
            </Button>
          )}
          {allSelectedSameArchiveGroup && archivedSelected.length > 0 && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-7 gap-1.5 text-xs"
              disabled={isBusy}
              onClick={handleUnarchive}
              data-testid="bulk-unarchive"
            >
              {bulkArchive.isPending ? (
                <Loader2Icon className="size-3 animate-spin" />
              ) : (
                <ArchiveRestoreIcon className="size-3" />
              )}
              Unarchive
            </Button>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className={cn("h-7 gap-1.5 text-xs", ownedSelected.length > 0 && "text-destructive")}
            disabled={isBusy || ownedSelected.length === 0}
            onClick={() => setConfirmDeleteOpen(true)}
            data-testid="bulk-delete"
          >
            {bulkDelete.isPending ? (
              <Loader2Icon className="size-3 animate-spin" />
            ) : (
              <Trash2Icon className="size-3" />
            )}
            Delete {ownedSelected.length > 0 ? ownedSelected.length : ""}
          </Button>
        </div>

        {(bulkArchive.isError || bulkDelete.isError) && (
          <p className="text-xs text-destructive" role="alert">
            Some actions failed. Retry or dismiss.
          </p>
        )}
      </div>

      <Dialog open={confirmDeleteOpen} onOpenChange={setConfirmDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {ownedSelected.length} session(s)?</DialogTitle>
            <DialogDescription>
              This will permanently delete the selected sessions and all their history. This cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <p className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-xs text-muted-foreground">
            <AlertTriangleIcon className="mt-0.5 size-3.5 shrink-0 text-warning" />
            Branches are not cleaned up. Use single-session delete for branch surgery.
          </p>
          <DialogFooter className="border-t-0 bg-transparent">
            <Button
              type="button"
              variant="ghost"
              onClick={() => setConfirmDeleteOpen(false)}
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

function readPinnedConversationIds(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(PINNED_CONVERSATION_IDS_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Migrate legacy prefixed ids (``conv_<hex>``) to the bare-hex form the API
    // returns post id-to-binary migration; the write-back effect re-persists
    // the migrated ids, so this one-time rewrite is durable across reloads.
    return migratePinnedConversationIds(
      parsed.filter((value): value is string => typeof value === "string"),
    );
  } catch {
    // Browser storage is user-editable and can contain stale/corrupt values.
    // Treat bad pin state as "no pins" instead of breaking navigation.
    return [];
  }
}

function writePinnedConversationIds(ids: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Pinning is a local navigation preference; storage failures should not
    // make the sidebar unusable.
  }
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

function sameStringArray(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}
