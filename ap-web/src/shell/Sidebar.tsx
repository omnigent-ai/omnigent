import {
  type CSSProperties,
  type KeyboardEvent,
  type MouseEvent,
  useCallback,
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
  ChevronRightIcon,
  CircleStopIcon,
  GitBranchIcon,
  InboxIcon,
  ListChecksIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  PanelRightOpenIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  SearchIcon,
  ShareIcon,
  SquareIcon,
  SquareCheckIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { Link, useLocation, useNavigate, useParams } from "@/lib/routing";
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  type Conversation,
  useArchiveConversation,
  useBulkArchiveConversations,
  useBulkDeleteConversations,
  useConversations,
  usePinnedConversationBackfill,
  useRenameConversation,
  useStopAndDeleteConversation,
  useStopSession,
} from "@/hooks/useConversations";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { PermissionsModal } from "@/components/PermissionsModal";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { useCommentInbox } from "@/hooks/useCommentInbox";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { isSessionStoppable } from "@/lib/sessionStop";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { getSessionState } from "@/hooks/useSessionState";
import { isConversationUnseen } from "@/hooks/useUnseenConversations";
import { sumPendingApprovals } from "@/lib/inbox";
import { cn } from "@/lib/utils";
import { useResizableSidebar } from "@/hooks/useResizableSidebar";
import { useSessionSwitchHotkey } from "@/hooks/useSessionSwitchHotkey";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { ThemeModeMenu } from "@/components/theme/ThemeModeMenu";
import { AccountMenu } from "./AccountMenu";
import {
  type ActiveChatOverride,
  COLLAPSED_SIDEBAR_SECTIONS_STORAGE_KEY,
  computeNextActiveOverride,
  conversationDisplayLabel,
  normalizePinnedConversationIds,
  PINNED_CONVERSATION_IDS_STORAGE_KEY,
  sortByUpdatedAtDesc,
  togglePinnedConversationId,
} from "./sidebarNav";

// Positioning shared by both occupants of a row's trailing time-marker slot
// (the session-state badge or the relative timestamp). On desktop the slot
// fades out on hover/focus so the pin + kebab controls can take its place;
// on mobile it sits left of the always-visible controls (right-[4.5rem]).
const TIME_MARKER_SLOT_CLASS =
  "-translate-y-1/2 pointer-events-none absolute top-1/2 right-[4.5rem] flex h-5 items-center transition-opacity md:right-2 md:group-hover:opacity-0 md:group-has-[:focus-visible]:opacity-0 md:group-has-[[aria-expanded=true]]:opacity-0";

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
export function Sidebar({ open, onClose, dragProgress = null }: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState("");
  const [pinnedConversationIds, setPinnedConversationIds] = useState(readPinnedConversationIds);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const toggleSelected = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
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
  }, []);

  // Debounce search input so we don't fire a server request on every
  // keystroke. 300 ms is fast enough to feel responsive.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearchQuery(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // One paginated session list — sessions are no longer split by
  // connection state, so the sidebar fetches a single undifferentiated
  // list. Archived sessions are included (`includeArchived: true`) and
  // peeled into their own "Archived" section at the bottom of the list.
  const conversationsQuery = useConversations(debouncedSearchQuery, true, {
    reconcileWhileConnected: true,
  });

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
        "conversations-sidebar flex flex-col bg-card",
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
          ? "md:m-2 md:w-[var(--sidebar-width)] md:rounded-xl md:border md:border-border md:shadow-lg"
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
      <div className="flex items-center justify-between px-4 pt-3">
        {/* Brand mark doubles as the "home" affordance: clicking it
            returns to `/`, the new-session composer. Without this there
            is no way back to the landing composer once you're inside a
            session. Reuses onNavClick so a plain primary click closes
            the sidebar on mobile (where it's a full-screen overlay) but
            modifier/middle clicks still open `/` in a new tab. */}
        <Link
          to="/"
          onClick={onNavClick}
          className="rounded-sm text-[15px] font-semibold tracking-tight text-foreground transition-colors hover:text-foreground/70"
        >
          Omnigent
        </Link>
        <div className="flex items-center gap-1">
          <ThemeModeMenu />
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Close sidebar"
                onClick={onClose}
                className="rounded-full"
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

      <div className="px-3 py-3">
        {/* "New session" routes to the home composer ("/"), which now owns
            session creation end-to-end (host/workspace/worktree chips +
            send). Rendered as a Link so cmd/middle-click opens it in a new
            tab; onNavClick still closes the sidebar on a plain mobile tap. */}
        <Button
          asChild
          className={cn(
            "w-full justify-start gap-2 text-sm",
            isNewChatPage && "bg-muted font-semibold",
          )}
          variant="ghost"
          data-testid="new-chat-button"
        >
          <Link to="/" onClick={onNavClick}>
            <PencilIcon className="size-4 text-muted-foreground" />
            New session
          </Link>
        </Button>
        <Button
          asChild
          className={cn(
            "w-full justify-start gap-2 text-sm",
            isInboxPage && "bg-muted font-semibold",
          )}
          variant="ghost"
          data-testid="inbox-button"
        >
          <Link to="/inbox" onClick={onNavClick}>
            <InboxIcon className="size-4" />
            Inbox
            {inboxCount > 0 && (
              <span
                aria-label={
                  inboxCount === 1 ? "1 inbox item waiting" : `${inboxCount} inbox items waiting`
                }
                className="ml-auto inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full bg-warning/15 px-1.5 text-[11px] font-medium text-warning tabular-nums"
              >
                {inboxCount}
              </span>
            )}
          </Link>
        </Button>
        {selectionMode ? (
          <BulkActionBar
            selectedIds={selectedIds}
            allConversations={(conversationsQuery.data?.pages ?? []).flatMap((page) => page.data)}
            onSelectAll={() =>
              selectAll((conversationsQuery.data?.pages ?? []).flatMap((page) => page.data))
            }
            onDeselectAll={deselectAll}
            onClear={deselectAll}
            onExit={exitSelectionMode}
          />
        ) : (
          <div className="relative mt-3 flex items-center gap-1.5">
            <div className="relative flex-1">
              <SearchIcon className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2.5 size-3.5 text-muted-foreground" />
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                aria-label="Search sessions"
                placeholder="Search sessions"
                className="min-h-8 w-full rounded-full border border-input pr-3 pl-8 text-sm transition placeholder:text-muted-foreground focus-visible:outline-1"
              />
            </div>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="Select sessions"
                  data-testid="toggle-selection-mode"
                  className="shrink-0 rounded-full"
                  onClick={() => setSelectionMode(true)}
                >
                  <ListChecksIcon className="size-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">Select sessions</TooltipContent>
            </Tooltip>
          </div>
        )}
      </div>

      <nav className="relative flex-1 overflow-y-auto px-3 pb-3 [scrollbar-gutter:stable]">
        <ConversationList
          conversationsQuery={conversationsQuery}
          onRowClick={onNavClick}
          searchQuery={debouncedSearchQuery}
          pinnedConversationIds={pinnedConversationIds}
          onPinnedConversationIdsChange={setPinnedConversationIds}
          onTogglePinned={togglePinnedConversation}
          selectionMode={selectionMode}
          selectedIds={selectedIds}
          onToggleSelected={toggleSelected}
        />
      </nav>

      {/* Account footer. Sibling *after* the flex-1 nav so it pins to the
          bottom of the sidebar column. Renders nothing (no border, no
          padding) when the accounts auth provider is off, so non-accounts
          deploys keep the conversation list flush to the bottom edge. */}
      <AccountMenu />
    </aside>
  );
}

interface ConversationListProps {
  conversationsQuery: ReturnType<typeof useConversations>;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  searchQuery: string;
  pinnedConversationIds: string[];
  onPinnedConversationIdsChange: (ids: string[]) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string) => void;
}

// permission_level null (no ACL row / legacy) or >= 4 both mean owner.
function isOwnedByViewer(conversation: Conversation): boolean {
  return isOwnerLevel(conversation.permission_level);
}

function ConversationList({
  conversationsQuery,
  onRowClick,
  searchQuery,
  pinnedConversationIds,
  onPinnedConversationIdsChange,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
}: ConversationListProps) {
  // All loaded conversations from the single paginated list (for pinned
  // backfill, normalization, and the flat session list).
  const allConversations = useMemo(
    () => conversationsQuery.data?.pages.flatMap((page) => page.data) ?? [],
    [conversationsQuery.data],
  );

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
  // the viewer's own sessions (Recent) and ones shared with them. Archived
  // sessions render in their own group at the bottom (below "Shared with
  // me"); a pinned-then-archived session shows under Archived, not Pinned.
  const pinnedSet = useMemo(() => new Set(pinnedConversationIds), [pinnedConversationIds]);
  const sections = useMemo(() => {
    const allWithBackfill = [...allConversations, ...pinnedBackfill];
    const pinned = sortByUpdatedAtDesc(
      allWithBackfill.filter((c) => pinnedSet.has(c.id) && c.archived !== true),
      activeOverride,
    );
    const pinnedIdSet = new Set(pinned.map((c) => c.id));
    const active = allConversations.filter((c) => !pinnedIdSet.has(c.id) && c.archived !== true);
    const sessions = sortByUpdatedAtDesc(active.filter(isOwnedByViewer), activeOverride);
    const shared = sortByUpdatedAtDesc(
      active.filter((c) => !isOwnedByViewer(c)),
      activeOverride,
    );
    const archived = sortByUpdatedAtDesc(
      allWithBackfill.filter((c) => c.archived === true),
      activeOverride,
    );
    return { pinned, sessions, shared, archived };
  }, [allConversations, pinnedBackfill, pinnedSet, activeOverride]);

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

  // Visible rows in render order (collapsed sections excluded) for the Cmd+↑/↓
  // session hotkey. Titles must match the <ConversationSection> props below.
  const orderedConversationIds = useMemo(() => {
    const visible = (title: string, list: readonly Conversation[]) =>
      collapsedSections.includes(title) ? [] : list;
    return [
      ...visible("Pinned", sections.pinned),
      ...visible("Recent", sections.sessions),
      ...visible("Shared with me", sections.shared),
      ...visible("Archived", sections.archived),
    ].map((c) => c.id);
  }, [sections, collapsedSections]);
  useSessionSwitchHotkey(orderedConversationIds, activeId);

  // Only normalize pinned ids once all pages are loaded; a pin that
  // lives on an unloaded page should not be dropped prematurely
  // (the backfill covers it in the meantime).
  const hasMorePages = conversationsQuery.hasNextPage;
  useEffect(() => {
    if (!conversationsQuery.data || hasMorePages || searchQuery) return;
    const allLoaded = [...allConversations, ...pinnedBackfill];
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
  const emptyMessage = searchQuery ? "No matching conversations" : "No active sessions";

  const totalVisible =
    sections.pinned.length +
    sections.sessions.length +
    sections.shared.length +
    sections.archived.length;

  // Section structure comes from the muted micro-headers + whitespace
  // alone (Linear-style) — no divider rules between groups.
  return (
    <div className="flex flex-col gap-3">
      {totalVisible === 0 ? (
        <p className="px-2 py-1 text-muted-foreground text-xs">{emptyMessage}</p>
      ) : (
        <>
          {sections.pinned.length > 0 && (
            <ConversationSection
              title="Pinned"
              conversations={sections.pinned}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
              selectionMode={selectionMode}
              selectedIds={selectedIds}
              onToggleSelected={onToggleSelected}
            />
          )}
          {sections.sessions.length > 0 && (
            <ConversationSection
              title="Recent"
              conversations={sections.sessions}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
              selectionMode={selectionMode}
              selectedIds={selectedIds}
              onToggleSelected={onToggleSelected}
            />
          )}
          {sections.shared.length > 0 && (
            <ConversationSection
              title="Shared with me"
              conversations={sections.shared}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
              selectionMode={selectionMode}
              selectedIds={selectedIds}
              onToggleSelected={onToggleSelected}
            />
          )}
          {sections.archived.length > 0 && (
            <ConversationSection
              title="Archived"
              conversations={sections.archived}
              pinnedConversationIds={pinnedConversationIds}
              collapsedSections={collapsedSections}
              onToggleCollapsed={toggleSectionCollapsed}
              onRowClick={onRowClick}
              onTogglePinned={onTogglePinned}
              selectionMode={selectionMode}
              selectedIds={selectedIds}
              onToggleSelected={onToggleSelected}
            />
          )}
          {/* Pagination extends the Recent list, so the button hides with
              it — a Load more under a collapsed group reads orphaned. */}
          {hasMorePages && !collapsedSections.includes("Recent") && (
            <button
              type="button"
              disabled={conversationsQuery.isFetchingNextPage}
              onClick={() => {
                if (conversationsQuery.hasNextPage) void conversationsQuery.fetchNextPage();
              }}
              className="flex cursor-pointer items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-muted-foreground text-xs hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
            >
              {conversationsQuery.isFetchingNextPage ? (
                <>
                  <Loader2Icon className="size-3 animate-spin" />
                  Loading…
                </>
              ) : (
                "Load more"
              )}
            </button>
          )}
        </>
      )}
    </div>
  );
}

function ConversationSection({
  title,
  conversations,
  pinnedConversationIds,
  collapsedSections,
  onToggleCollapsed,
  onRowClick,
  onTogglePinned,
  selectionMode,
  selectedIds,
  onToggleSelected,
}: {
  title?: string;
  conversations: Conversation[];
  pinnedConversationIds: string[];
  collapsedSections: string[];
  onToggleCollapsed: (sectionTitle: string) => void;
  onRowClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  selectedIds: Set<string>;
  onToggleSelected: (conversationId: string) => void;
}) {
  const collapsed = title != null && collapsedSections.includes(title);
  return (
    <section>
      {title && (
        <h2>
          <button
            type="button"
            aria-expanded={!collapsed}
            onClick={() => onToggleCollapsed(title)}
            className="group flex w-full items-center gap-1 rounded-md px-2 py-1 text-left text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {title}
            <ChevronRightIcon
              className={cn(
                "size-3.5 shrink-0 transition-transform",
                !collapsed && "rotate-90 opacity-0 group-hover:opacity-100",
              )}
            />
          </button>
        </h2>
      )}
      {!collapsed && (
        <ul className="flex flex-col gap-0.5">
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
            />
          ))}
        </ul>
      )}
    </section>
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
}: {
  conversation: Conversation;
  isPinned: boolean;
  onClick: (e: MouseEvent<HTMLAnchorElement>) => void;
  onTogglePinned: (conversationId: string) => void;
  selectionMode: boolean;
  isSelected: boolean;
  onToggleSelected: (conversationId: string) => void;
}) {
  // `useParams` reads from the active matched route. On `/`, the param is
  // undefined; on `/c/:conversationId`, it carries the active id.
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const isActive = activeId === conversation.id;
  const navigate = useNavigate();
  // Track the *live* active conversation id. Delete is fire-and-forget,
  // so the user can navigate to another conversation before the mutation
  // resolves — the onSuccess redirect must key off where they are now,
  // not the `isActive` captured when delete was initiated.
  const activeIdRef = useRef(activeId);
  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);
  const rename = useRenameConversation();
  const del = useStopAndDeleteConversation();
  const archive = useArchiveConversation();
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
  // Opt-in "delete local branch" checkbox (worktree sessions only).
  const [deleteBranch, setDeleteBranch] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  // True while an archive is in flight. Drives the "Archiving…" status
  // row, mirroring delete's "Deleting…" indicator — without it the row
  // shows nothing while the archive completes.
  const [isArchiving, setIsArchiving] = useState(false);
  const gitBranch = conversation.git_branch ?? null;
  const isOwner = isOwnedByViewer(conversation);
  const canEdit = conversation.permission_level === null || conversation.permission_level >= 2;
  const canManage = conversation.permission_level === null || conversation.permission_level >= 3;
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

  const label = conversationDisplayLabel(conversation);
  const hasUnseenMessages =
    !isActive &&
    isConversationUnseen(conversation.id, conversation.updated_at, conversation.status);
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
          { onSettled: () => setIsArchiving(false) },
        );
      },
    });
  }

  return (
    <li className="group relative">
      <Link
        to={selectionMode ? "#" : `/c/${conversation.id}`}
        className={cn(
          "relative flex w-full flex-col gap-0.5 rounded-md px-4 py-2 text-left text-sm hover:bg-muted",
          !selectionMode &&
            (sessionState?.kind === "awaiting" ? "pr-44 md:pr-28" : "pr-28 md:pr-16"),
          selectionMode && "pr-10",
          isActive && "bg-muted font-semibold",
          selectionMode && isSelected && "bg-primary/5",
        )}
        onClick={(e) => {
          if (selectionMode) {
            e.preventDefault();
            e.stopPropagation();
            onToggleSelected(conversation.id);
            return;
          }
          onClick(e);
        }}
        onDoubleClick={(e) => {
          if (selectionMode) return;
          if (!canEdit) return;
          e.preventDefault();
          setIsEditing(true);
        }}
        title={conversation.title ?? conversation.id}
      >
        {/* Row 1: the session name. Status markers (working, needs-approval,
            unseen) render in the trailing time-marker slot below, replacing
            the timestamp — not inline here. Leading icons (agent type, pin,
            shared) were removed to keep rows text-clean; pinned rows still
            group under "Pinned". */}
        <div className="flex w-full items-center gap-1.5">
          <span className={cn("relative min-w-0 truncate", hasUnseenMessages && "font-semibold")}>
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
      {selectionMode ? (
        <span className="-translate-y-1/2 pointer-events-none absolute top-1/2 right-2.5 flex items-center">
          {isSelected ? (
            <SquareCheckIcon className="size-4 text-primary" />
          ) : (
            <SquareIcon className="size-4 text-muted-foreground" />
          )}
        </span>
      ) : sessionState !== null ? (
        <span className={TIME_MARKER_SLOT_CLASS}>
          <SessionStateBadge state={sessionState} />
        </span>
      ) : (
        <span
          className={cn(TIME_MARKER_SLOT_CLASS, "text-xs tabular-nums text-muted-foreground")}
          aria-label={absoluteTime(conversation.updated_at * 1000)}
          title={absoluteTime(conversation.updated_at * 1000)}
        >
          {relativeTime(conversation.updated_at * 1000)}
        </span>
      )}
      {!selectionMode && (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={isPinned ? "Unpin conversation" : "Pin conversation"}
          data-testid="quick-pin-conversation"
          className={cn(
            "-translate-y-1/2 absolute top-1/2 right-9 transition-opacity",
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
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label="Conversation actions"
              data-testid="conversation-actions"
              // Absolute-positioned trigger. On mobile (no hover state)
              // it's always visible. On desktop it stays hidden until
              // hover / keyboard focus, with `aria-expanded` keeping it
              // surfaced while the menu is open so the trigger doesn't
              // vanish under the cursor.
              className={cn(
                "-translate-y-1/2 absolute top-1/2 right-1 transition-opacity",
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
          <DropdownMenuContent align="end" className="min-w-36">
            {isOwner ? (
              <DropdownMenuItem data-testid="archive-conversation" onSelect={runArchive}>
                {isArchived ? (
                  <ArchiveRestoreIcon className="size-3.5" />
                ) : (
                  <ArchiveIcon className="size-3.5" />
                )}
                {isArchived ? "Unarchive" : "Archive"}
              </DropdownMenuItem>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <div>
                    <DropdownMenuItem data-testid="archive-conversation" disabled>
                      {isArchived ? (
                        <ArchiveRestoreIcon className="size-3.5" />
                      ) : (
                        <ArchiveIcon className="size-3.5" />
                      )}
                      {isArchived ? "Unarchive" : "Archive"}
                    </DropdownMenuItem>
                  </div>
                </TooltipTrigger>
                <TooltipContent side="left">
                  Only the session owner can {isArchived ? "unarchive" : "archive"} this session
                </TooltipContent>
              </Tooltip>
            )}
            {canManage ? (
              <DropdownMenuItem
                data-testid="share-conversation"
                onSelect={() => setShareOpen(true)}
              >
                <ShareIcon className="size-3.5" />
                Share
              </DropdownMenuItem>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <div>
                    <DropdownMenuItem data-testid="share-conversation" disabled>
                      <ShareIcon className="size-3.5" />
                      Share
                    </DropdownMenuItem>
                  </div>
                </TooltipTrigger>
                <TooltipContent side="left">
                  You need manage permissions to share this session
                </TooltipContent>
              </Tooltip>
            )}
            {canEdit ? (
              <DropdownMenuItem
                data-testid="rename-conversation"
                onSelect={() => setIsEditing(true)}
              >
                <PencilIcon className="size-3.5" />
                Rename
              </DropdownMenuItem>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <div>
                    <DropdownMenuItem data-testid="rename-conversation" disabled>
                      <PencilIcon className="size-3.5" />
                      Rename
                    </DropdownMenuItem>
                  </div>
                </TooltipTrigger>
                <TooltipContent side="left">
                  You need edit permissions to rename this session
                </TooltipContent>
              </Tooltip>
            )}
            {/* Stop session — only on stoppable sessions whose runner isn't
              already known-offline (canStop). Owner-gated like Delete:
              non-owners see it disabled with an explanatory tooltip. */}
            {canStop &&
              (isOwner ? (
                <DropdownMenuItem
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
                </DropdownMenuItem>
              ) : (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <div>
                      <DropdownMenuItem data-testid="stop-conversation" disabled>
                        <CircleStopIcon className="size-3.5" />
                        Stop session
                      </DropdownMenuItem>
                    </div>
                  </TooltipTrigger>
                  <TooltipContent side="left">
                    Only the session owner can stop this session
                  </TooltipContent>
                </Tooltip>
              ))}
            {isOwner ? (
              <DropdownMenuItem
                data-testid="delete-conversation"
                variant="destructive"
                onSelect={() => setDeleteOpen(true)}
              >
                <Trash2Icon className="size-3.5" />
                Delete
              </DropdownMenuItem>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <div>
                    <DropdownMenuItem data-testid="delete-conversation" disabled>
                      <Trash2Icon className="size-3.5" />
                      Delete
                    </DropdownMenuItem>
                  </div>
                </TooltipTrigger>
                <TooltipContent side="left">
                  Only the session owner can delete this session
                </TooltipContent>
              </Tooltip>
            )}
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
            // 503 = runner couldn't deliver the kill; keep the dialog open.
            <p className="text-sm text-destructive" role="alert">
              Couldn't stop the session — it may still be running. Try again in a moment.
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

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
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
    // pl-3 + the input's px-1 line the text up with the row's px-4 title;
    // py-1 around the size-7 buttons matches the 36px single-line row height.
    <div className="flex items-center gap-1 rounded-md bg-muted py-1 pr-1 pl-3">
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        data-testid="rename-conversation-input"
        className="min-w-0 flex-1 truncate rounded bg-transparent px-1 py-1 text-sm outline-none"
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
  onSelectAll,
  onDeselectAll,
  onClear,
  onExit,
}: {
  selectedIds: Set<string>;
  allConversations: Conversation[];
  onSelectAll: () => void;
  onDeselectAll: () => void;
  onClear: () => void;
  onExit: () => void;
}) {
  const navigate = useNavigate();
  const { conversationId: activeId } = useParams<{ conversationId: string }>();
  const bulkArchive = useBulkArchiveConversations();
  const bulkDelete = useBulkDeleteConversations();

  const selectedConversations = useMemo(
    () => allConversations.filter((c) => selectedIds.has(c.id)),
    [allConversations, selectedIds],
  );

  const ownedSelected = useMemo(
    () => selectedConversations.filter((c) => isOwnedByViewer(c)),
    [selectedConversations],
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
  const allSelected = count > 0 && count === allConversations.length;
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
          <div className="flex items-center gap-1.5 md:hidden">
            {allSelectedSameArchiveGroup && nonArchivedSelected.length > 0 && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-7 gap-1.5 text-xs"
                disabled={isBusy}
                onClick={handleArchive}
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
            >
              {bulkDelete.isPending ? (
                <Loader2Icon className="size-3 animate-spin" />
              ) : (
                <Trash2Icon className="size-3" />
              )}
              Delete {ownedSelected.length > 0 ? ownedSelected.length : ""}
            </Button>
          </div>
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

        <div className="hidden items-center gap-1.5 px-2 md:flex">
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
    return parsed.filter((value): value is string => typeof value === "string");
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

// Archived starts collapsed until the user touches any section header —
// once they do, the stored array (even an empty one) is the preference.
const DEFAULT_COLLAPSED_SIDEBAR_SECTIONS = ["Archived"];

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

function sameStringArray(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}
