// A project's sessions as cards in the sidebar's Status columns. Columns follow
// live status; dragging a card between Unread and Done marks it read or unread.

import {
  createContext,
  Fragment,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  DndContext,
  DragOverlay,
  MouseSensor,
  TouchSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  FolderGit2Icon,
  GitBranchIcon,
  LaptopIcon,
  ListChevronsUpDownIcon,
  MoreHorizontalIcon,
  SendHorizontalIcon,
  type LucideIcon,
} from "lucide-react";
import { RunningDot } from "@/components/RunningDot";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  useMoveToProject,
  useProjectSessions,
  useProjects,
  type Conversation,
} from "@/hooks/useConversations";
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useHosts, type Host } from "@/hooks/useHosts";
import { getSessionState } from "@/hooks/useSessionState";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useOptionalLoadedConversations, useOptionalSidebarShow } from "@/hooks/useSidebarData";
import {
  isConversationUnseen,
  markConversationRead,
  markConversationUnread,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { isSingleUserMode } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { fetchLastAssistantMessage, leadingSentences } from "@/lib/lastAssistantText";
import { Link } from "@/lib/routing";
import { hasSessionDraft, useSessionDraftIds } from "@/lib/sessionDrafts";
import { isSessionStoppable } from "@/lib/sessionStop";
import { cn } from "@/lib/utils";
import { RenameSessionDialog } from "@/shell/RenameSessionDialog";
import { ConversationMenuItems } from "@/shell/Sidebar";
import {
  conversationDisplayLabel,
  isOwnedByViewer,
  statusBucket,
  type StatusBucket,
} from "@/shell/sidebarNav";
import { useSessionActions } from "@/shell/useSessionActions";
import {
  SessionEnvironmentLabel,
  SessionHarnessIcon,
  sessionRepoName,
  SessionUpdatedLabel,
  useSessionBranch,
} from "@/shell/sessionRowMeta";
import {
  filterKanbanCards,
  KANBAN_COLUMNS,
  kanbanColumns,
  kanbanDropAction,
  projectBoardRows,
  takeArrivals,
  type KanbanCard,
} from "./projectKanban";

// A drag that ends over its own card still fires a click; ignore it this long.
const DRAG_CLICK_SUPPRESS_MS = 250;
const RESPONSE_PREVIEW_SENTENCES = 2;
// Room left between a lane tab scrolled into view and the strip's edge, matching its padding.
const LANE_TAB_EDGE_PX = 16;

// Lane title tags, tinted after the sidebar's status markers.
const COLUMN_TAG_CLASS: Record<StatusBucket, string> = {
  Draft: "bg-muted text-muted-foreground",
  "Needs attention": "bg-warning/15 text-warning",
  Unread: "bg-brand-accent/15 text-brand-accent",
  Working: "bg-session-active/15 text-session-active",
  Done: "bg-success/15 text-success",
};

// Host metadata for the cards' Environment, resolved once by the board.
const HostsByIdContext = createContext<ReadonlyMap<string, Host>>(new Map());
// Cards that just joined the board, highlighted for ARRIVAL_HIGHLIGHT_MS.
const ArrivedIdsContext = createContext<ReadonlySet<string>>(new Set());
const ARRIVAL_HIGHLIGHT_MS = 2000;

/** Ids of sessions created since the board first loaded, each for a moment. */
function useArrivedIds(rows: readonly Conversation[], loaded: boolean): ReadonlySet<string> {
  const known = useRef<Set<string> | null>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const [arrived, setArrived] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    if (!loaded) return;
    if (known.current === null) {
      known.current = new Set(rows.map((row) => row.id));
      return;
    }
    const arrivals = takeArrivals(rows, known.current, Date.now() / 1000);
    if (arrivals.length === 0) return;
    setArrived((previous) => new Set([...previous, ...arrivals]));
    timers.current.push(
      setTimeout(() => {
        setArrived((previous) => new Set([...previous].filter((id) => !arrivals.includes(id))));
      }, ARRIVAL_HIGHLIGHT_MS),
    );
  }, [rows, loaded]);
  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(clearTimeout);
  }, []);
  return arrived;
}

function isUnseen(row: Conversation): boolean {
  return isConversationUnseen(row.id, row.updated_at, row.status);
}

/** Marks every unread session on the card read, so it leaves Unread. */
function markCardRead(card: KanbanCard) {
  for (const member of card.members) {
    if (isUnseen(member)) markConversationRead(member.id, member.updated_at);
  }
}

function markCardUnread(card: KanbanCard) {
  markConversationUnread(card.conversation.id, card.conversation.updated_at);
}

function columnOf(row: Conversation): StatusBucket {
  return statusBucket(row, isUnseen(row), hasSessionDraft(row.id));
}

export function ProjectKanbanBoard({
  projectName,
  searchQuery = "",
  quickViewHref,
  selectedSessionId = null,
  onCloseQuickView,
  overlayClearance,
}: {
  projectName: string;
  /** Shows only cards with a session whose title or id contains it. */
  searchQuery?: string;
  /** Where each card's quick-view button links; without it, cards have none. */
  quickViewHref?: (conversationId: string) => string;
  /** The session open in quick view, marked on its card. */
  selectedSessionId?: string | null;
  /** Closes quick view, e.g. when its session is deleted or archived from its card. */
  onCloseQuickView?: () => void;
  /** Room below each lane's last card, so it can scroll clear of an overlay such as the composer. */
  overlayClearance?: string;
}) {
  const viewerId = useViewerId();
  const isMobile = useIsMobileViewport();
  const { data: projects } = useProjects();
  const projectId = projects?.find((project) => project.name === projectName)?.id ?? null;
  const query = useProjectSessions(projectName, true);
  const loaded = useOptionalLoadedConversations();
  // Read marks and drafts live outside the query cache; re-render when they change.
  useUnseenTick();
  useSessionDraftIds();
  const rows = useMemo(
    () =>
      projectBoardRows(
        query.data?.pages.flatMap((page) => page.data) ?? [],
        loaded?.pages.flatMap((page) => page.data) ?? [],
        { id: projectId, name: projectName },
        viewerId,
      ),
    [query.data, loaded, projectId, projectName, viewerId],
  );
  const columns = kanbanColumns(rows, columnOf);
  const arrivedIds = useArrivedIds(rows, query.isSuccess);
  const showEnvironment = useOptionalSidebarShow().includes("environment");
  const { data: hosts } = useHosts({ includeSandbox: true, enabled: showEnvironment });
  const hostsById = useMemo(
    () => new Map((hosts ?? []).map((host) => [host.host_id, host] as const)),
    [hosts],
  );

  const [dragged, setDragged] = useState<KanbanCard | null>(null);
  const dragEndedAt = useRef(Number.NEGATIVE_INFINITY);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );

  if (query.isPending) {
    return (
      <div className="flex flex-1 justify-center py-10">
        <Spinner className="size-5 text-muted-foreground" aria-label="Loading sessions" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div role="alert" className="flex flex-1 flex-col items-center gap-3 p-6">
        <p className="text-muted-foreground">Couldn’t load this project’s sessions.</p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const visibleCards = new Map(
    KANBAN_COLUMNS.map((column) => [column, filterKanbanCards(columns.get(column)!, searchQuery)]),
  );
  const renderColumn = (column: StatusBucket, inTab = false) => (
    <KanbanColumn
      key={column}
      inTab={inTab}
      projectName={projectName}
      column={column}
      cards={visibleCards.get(column)!}
      emptyLabel={searchQuery.trim() ? "No matching sessions" : "No sessions"}
      dropAllowed={dragged !== null && kanbanDropAction(dragged, column) !== null}
      suppressClick={() => Date.now() - dragEndedAt.current < DRAG_CLICK_SUPPRESS_MS}
      quickViewHref={quickViewHref}
      selectedSessionId={selectedSessionId}
      onCloseQuickView={onCloseQuickView}
      overlayClearance={overlayClearance}
    />
  );

  return (
    <HostsByIdContext.Provider value={hostsById}>
      <ArrivedIdsContext.Provider value={arrivedIds}>
        <DndContext
          sensors={sensors}
          onDragStart={({ active }) => setDragged(active.data.current?.card ?? null)}
          onDragCancel={() => setDragged(null)}
          onDragEnd={({ over }) => {
            const card = dragged;
            setDragged(null);
            dragEndedAt.current = Date.now();
            const target = over?.data.current?.column as StatusBucket | undefined;
            if (!card || !target) return;
            const action = kanbanDropAction(card, target);
            if (action === "mark_read") markCardRead(card);
            else if (action === "mark_unread") markCardUnread(card);
          }}
        >
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            <div
              role="region"
              aria-label={`${projectName} kanban board`}
              data-testid="project-kanban-board"
              className={
                isMobile
                  ? "flex min-h-0 flex-1 flex-col"
                  : "flex min-h-0 flex-1 justify-center-safe gap-3 overflow-x-auto overflow-y-hidden px-4 pt-1 pb-4"
              }
            >
              {isMobile ? (
                <KanbanLaneTabs
                  cardCounts={
                    new Map(
                      KANBAN_COLUMNS.map((column) => [column, visibleCards.get(column)!.length]),
                    )
                  }
                  renderLane={(column) => renderColumn(column, true)}
                />
              ) : (
                KANBAN_COLUMNS.map((column) => renderColumn(column))
              )}
            </div>
            {query.hasNextPage && (
              <div className="flex shrink-0 justify-center pb-4">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={query.isFetchingNextPage}
                  onClick={() => void query.fetchNextPage()}
                >
                  {query.isFetchingNextPage ? "Loading…" : "Load more sessions"}
                </Button>
              </div>
            )}
          </div>
          <DragOverlay dropAnimation={null}>
            {dragged && <KanbanCardBody card={dragged} className="w-64 rotate-1 shadow-xl" />}
          </DragOverlay>
        </DndContext>
      </ArrivedIdsContext.Provider>
    </HostsByIdContext.Provider>
  );
}

/** Phones show one lane at a time, picked from underline tabs; opens on the first lane with cards. */
export function KanbanLaneTabs({
  cardCounts,
  renderLane,
}: {
  cardCounts: ReadonlyMap<StatusBucket, number>;
  renderLane: (column: StatusBucket) => ReactNode;
}) {
  const [lane, setLane] = useState<StatusBucket>(
    () => KANBAN_COLUMNS.find((column) => (cardCounts.get(column) ?? 0) > 0) ?? KANBAN_COLUMNS[0],
  );
  const stripRef = useRef<HTMLDivElement>(null);
  // Scroll the selected tab into view, on load included, clear of the strip's edge.
  useLayoutEffect(() => {
    const strip = stripRef.current;
    const tab = strip?.querySelector<HTMLElement>('[role="tab"][data-state="active"]');
    if (!strip || !tab) return;
    const stripBox = strip.getBoundingClientRect();
    const tabBox = tab.getBoundingClientRect();
    if (tabBox.left < stripBox.left) {
      strip.scrollLeft -= stripBox.left - tabBox.left + LANE_TAB_EDGE_PX;
    } else if (tabBox.right > stripBox.right) {
      strip.scrollLeft += tabBox.right - stripBox.right + LANE_TAB_EDGE_PX;
    }
  }, [lane]);
  return (
    <Tabs
      value={lane}
      onValueChange={(value) => setLane(value as StatusBucket)}
      componentId="project_kanban.lane_tabs"
      className="min-h-0 flex-1 flex-col gap-0"
    >
      <div
        ref={stripRef}
        className="no-scrollbar shrink-0 overflow-x-auto overflow-y-hidden border-b border-border"
      >
        <TabsList
          variant="line"
          aria-label="Lanes"
          className="h-10 w-max min-w-full justify-start gap-5 rounded-none px-4 py-0"
        >
          {KANBAN_COLUMNS.map((column) => (
            // The scrolling strip would spill the shared underline (drawn below the tab) and
            // clip a focus ring, so the active or focused tab draws its own bottom border.
            <TabsTrigger
              key={column}
              value={column}
              data-testid={`kanban-lane-tab-${column}`}
              className="h-full flex-none rounded-none border-0 border-b-2 border-transparent px-0 after:hidden focus-visible:border-foreground focus-visible:ring-0 focus-visible:outline-none data-[state=active]:border-foreground data-[state=active]:text-foreground dark:data-[state=active]:text-foreground"
            >
              {column}
              <Badge className="border-transparent bg-muted text-muted-foreground tabular-nums">
                {cardCounts.get(column)}
              </Badge>
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
      {KANBAN_COLUMNS.map((column) => (
        <TabsContent key={column} value={column} className="flex min-h-0 flex-col">
          {renderLane(column)}
        </TabsContent>
      ))}
    </Tabs>
  );
}

function KanbanColumn({
  inTab = false,
  projectName,
  column,
  cards,
  emptyLabel,
  dropAllowed,
  suppressClick,
  quickViewHref,
  selectedSessionId,
  onCloseQuickView,
  overlayClearance,
}: {
  /** Fills a lane tab, whose trigger already names it, instead of sitting beside the other lanes. */
  inTab?: boolean;
  projectName: string;
  column: StatusBucket;
  cards: KanbanCard[];
  emptyLabel: string;
  /** Whether the card being dragged can drop here. */
  dropAllowed: boolean;
  suppressClick: () => boolean;
  quickViewHref?: (conversationId: string) => string;
  selectedSessionId: string | null;
  onCloseQuickView?: () => void;
  overlayClearance?: string;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `column:${column}`, data: { column } });
  return (
    <section
      ref={setNodeRef}
      aria-label={column}
      data-testid={`kanban-column-${column}`}
      className={cn(
        inTab
          ? "flex min-h-0 flex-1 flex-col bg-muted/30 px-4 pt-3"
          : "flex max-w-[calc(440px+1rem)] min-w-52 flex-1 basis-0 flex-col rounded-xl border border-transparent bg-muted/30 px-2 pt-2 transition-colors",
        dropAllowed && "border-dashed border-muted-foreground/50",
        dropAllowed && isOver && "border-primary bg-primary/10",
      )}
    >
      {!inTab && (
        <h2 className="flex shrink-0 items-center justify-between px-1.5 pt-1.5 pb-3 text-sm font-medium text-muted-foreground">
          <Badge className={cn("border-transparent", COLUMN_TAG_CLASS[column])}>{column}</Badge>
          <span className="tabular-nums">{cards.length}</span>
        </h2>
      )}
      {/* The lane's own scroller; its side padding keeps card rings from clipping. */}
      <ul
        className={cn(
          "-mx-2 flex min-h-0 flex-1 flex-col overflow-y-auto px-2 pt-0.5 pb-2",
          inTab ? "gap-4 sm:grid sm:grid-cols-2 sm:content-start" : "gap-3",
        )}
        style={{ paddingBottom: overlayClearance }}
      >
        {cards.map((card) => (
          <li key={card.conversation.id}>
            <DraggableKanbanCard
              card={card}
              projectName={projectName}
              hideAwaitingTag={column === "Needs attention"}
              suppressClick={suppressClick}
              quickViewHref={quickViewHref?.(card.conversation.id)}
              selected={card.conversation.id === selectedSessionId}
              onCloseQuickView={onCloseQuickView}
            />
          </li>
        ))}
        {cards.length === 0 && (
          <li className="px-1.5 py-1 text-sm text-muted-foreground">{emptyLabel}</li>
        )}
      </ul>
    </section>
  );
}

function DraggableKanbanCard({
  card,
  projectName,
  hideAwaitingTag,
  suppressClick,
  quickViewHref,
  selected,
  onCloseQuickView,
}: {
  card: KanbanCard;
  projectName: string;
  hideAwaitingTag: boolean;
  suppressClick: () => boolean;
  quickViewHref?: string;
  selected: boolean;
  onCloseQuickView?: () => void;
}) {
  const { setNodeRef, listeners, isDragging } = useDraggable({
    id: card.conversation.id,
    data: { card },
  });
  // Touch screens have no hover to reveal the actions menu.
  const menuAlwaysShown = useIsMobileViewport();
  return (
    <div className={cn("group/card relative", isDragging && "opacity-40")}>
      {/* Listeners only: the card keeps its link semantics rather than dnd-kit's button role. */}
      <Link
        ref={setNodeRef}
        {...listeners}
        to={`/c/${card.conversation.id}`}
        draggable={false}
        data-testid="kanban-card"
        onClick={(event) => {
          if (suppressClick()) event.preventDefault();
        }}
        className="block rounded-xl focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <KanbanCardBody
          card={card}
          hideAwaitingTag={hideAwaitingTag}
          reserveCornerAction={quickViewHref !== undefined}
          reserveMenu={menuAlwaysShown ? "always" : "hover"}
          className={cn(
            "group-hover/card:border-foreground/25",
            selected && "border-foreground/40 group-hover/card:border-foreground/40",
          )}
        />
      </Link>
      {/* Siblings of the card's link, since links can't nest interactive content. */}
      {!isDragging && (
        <KanbanCardMenu
          card={card}
          projectName={projectName}
          alwaysShown={menuAlwaysShown}
          onLeave={selected ? onCloseQuickView : undefined}
        />
      )}
      {quickViewHref !== undefined && !isDragging && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              asChild
              variant="ghost"
              size="icon-sm"
              className={cn(
                "absolute right-2 bottom-2 text-muted-foreground",
                !selected && "opacity-0 group-hover/card:opacity-100 focus-visible:opacity-100",
              )}
            >
              <Link
                to={quickViewHref}
                draggable={false}
                aria-label={`Quick view ${conversationDisplayLabel(card.conversation)}`}
                data-testid="kanban-card-quick-view"
              >
                <SendHorizontalIcon />
              </Link>
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Quick view</TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}

/** The card's actions menu: the sidebar row's actions that change the board. */
function KanbanCardMenu({
  card,
  projectName,
  alwaysShown,
  onLeave,
}: {
  card: KanbanCard;
  projectName: string;
  /** Shows the trigger without waiting for the card's hover. */
  alwaysShown: boolean;
  /** Called right before the session is deleted, archived, or left. */
  onLeave?: () => void;
}) {
  const { conversation } = card;
  const viewerId = useViewerId();
  const serverInfo = useServerInfo();
  const moveToProject = useMoveToProject();
  const runnerOnline = useSessionRunnerOnline(conversation.id);
  const [menuOpen, setMenuOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const label = conversationDisplayLabel(conversation);
  const actions = useSessionActions({
    conversation,
    label,
    viewerId,
    serverInfo,
    onLeave,
    componentIdPrefix: "project_kanban.card",
  });
  const canStop =
    isSessionStoppable({
      labels: conversation.labels,
      hostId: conversation.host_id,
      runnerId: conversation.runner_id,
    }) && runnerOnline !== false;
  return (
    <>
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`Actions for ${label}`}
            data-card-menu=""
            data-testid="kanban-card-actions"
            className={cn(
              "absolute top-2.5 right-2 text-muted-foreground",
              !alwaysShown &&
                "opacity-0 group-hover/card:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100",
            )}
          >
            <MoreHorizontalIcon className="size-3.5" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-44">
          <ConversationMenuItems
            kanbanCard
            conversation={conversation}
            isArchived={false}
            isOwner={isOwnedByViewer(conversation, viewerId)}
            sharingOff={serverInfo !== "loading" && serverInfo.sharing_mode === "off"}
            isSingleUser={isSingleUserMode(serverInfo)}
            canStop={canStop}
            canMarkUnread={!card.members.some(isUnseen)}
            currentProject={projectName}
            onMarkUnread={() => markCardUnread(card)}
            onMarkRead={() => markCardRead(card)}
            moveToProject={moveToProject}
            setShareOpen={actions.setShareOpen}
            setForkOpen={actions.setForkOpen}
            canCreateAutomation={false}
            setAutomationOpen={actions.setAutomationOpen}
            setIsEditing={setRenameOpen}
            setStopOpen={actions.setStopOpen}
            setDeleteOpen={actions.setDeleteOpen}
            setLeaveOpen={actions.setLeaveOpen}
            setMenuOpen={setMenuOpen}
            runArchive={actions.runArchive}
          />
        </DropdownMenuContent>
      </DropdownMenu>
      {actions.dialogs}
      {renameOpen && (
        <RenameSessionDialog conversation={conversation} open onOpenChange={setRenameOpen} />
      )}
    </>
  );
}

/**
 * The opening sentences of the session's last model response. Refetched once
 * per finished turn; a running turn keeps showing the previous response.
 */
function useLastResponsePreview(conversation: Conversation): string | undefined {
  const { data } = useQuery({
    queryKey: ["kanban-last-response", conversation.id, conversation.updated_at],
    queryFn: async () => (await fetchLastAssistantMessage(conversation.id)) ?? null,
    enabled: conversation.status !== "running",
    staleTime: Infinity,
    placeholderData: keepPreviousData,
  });
  return data ? leadingSentences(data, RESPONSE_PREVIEW_SENTENCES) : undefined;
}

function KanbanCardBody({
  card,
  hideAwaitingTag = false,
  reserveCornerAction = false,
  reserveMenu,
  className,
}: {
  card: KanbanCard;
  /** Drops the "Needs response" tag where the lane title already says it. */
  hideAwaitingTag?: boolean;
  /** Keeps the bottom-right corner clear for a button laid over the card. */
  reserveCornerAction?: boolean;
  /** Makes way at the top-right for the actions menu, while it shows on hover or for good. */
  reserveMenu?: "hover" | "always";
  className?: string;
}) {
  const { conversation, members } = card;
  const state =
    getSessionState(conversation) ?? (isUnseen(conversation) ? { kind: "unseen" as const } : null);
  const working = state?.kind === "running";
  const nestedCount = members.length - 1;
  const responsePreview = useLastResponsePreview(conversation);
  const show = useOptionalSidebarShow();
  const hostsById = useContext(HostsByIdContext);
  const arrived = useContext(ArrivedIdsContext).has(conversation.id);
  const repo = show.includes("repo") ? sessionRepoName(conversation) : null;
  const branch = useSessionBranch(conversation, show.includes("branch"));
  const metaParts: { key: string; icon?: LucideIcon; value: ReactNode }[] = [];
  if (show.includes("updated")) {
    metaParts.push({
      key: "updated",
      value: <SessionUpdatedLabel updatedAt={conversation.updated_at} />,
    });
  }
  if (nestedCount > 0) {
    metaParts.push({ key: "nested", icon: ListChevronsUpDownIcon, value: `${nestedCount} nested` });
  }
  if (show.includes("environment")) {
    metaParts.push({
      key: "environment",
      icon: LaptopIcon,
      value: <SessionEnvironmentLabel conversation={conversation} hostsById={hostsById} />,
    });
  }
  if (repo !== null) metaParts.push({ key: "repo", icon: FolderGit2Icon, value: repo });
  if (branch !== null) metaParts.push({ key: "branch", icon: GitBranchIcon, value: branch });
  return (
    <div
      className={cn(
        "flex flex-col gap-1.5 rounded-xl border border-border/60 bg-card p-3 text-ui text-foreground shadow-lg transition-[color,background-color,box-shadow] duration-700",
        arrived && "border-primary/60 ring-2 ring-primary/20",
        className,
      )}
    >
      <div
        className={cn(
          "flex min-w-0 items-start gap-2",
          reserveMenu === "hover" &&
            "group-hover/card:pr-6 group-has-[[data-card-menu][data-state=open]]/card:pr-6",
          reserveMenu === "always" && "pr-6",
        )}
      >
        {show.includes("harness") && (
          <span className="flex h-5 shrink-0 items-center">
            <SessionHarnessIcon
              conversation={conversation}
              testId="kanban-card-harness"
              withFallback
            />
          </span>
        )}
        <span className="min-w-0 flex-1 truncate font-medium">
          {conversationDisplayLabel(conversation)}
        </span>
        {state && !working && !(hideAwaitingTag && state.kind === "awaiting") && (
          <span
            className={cn(
              "flex shrink-0",
              reserveMenu === "hover" &&
                "group-hover/card:hidden group-has-[[data-card-menu][data-state=open]]/card:hidden",
            )}
          >
            <SessionStateBadge state={state} />
          </span>
        )}
      </div>
      {/* Always two lines tall, so every card keeps one shape. */}
      <div className="h-[2lh] text-sm text-muted-foreground">
        {working ? (
          <span
            role="status"
            aria-label="Working"
            data-testid="kanban-card-working"
            className="flex h-full items-center"
          >
            <RunningDot className="size-4" />
          </span>
        ) : (
          responsePreview && (
            <p data-testid="kanban-card-response" className="line-clamp-2">
              {responsePreview}
            </p>
          )
        )}
      </div>
      {(metaParts.length > 0 || reserveCornerAction) && (
        <div
          className={cn(
            "mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm text-primary-cta",
            reserveCornerAction && "min-h-4 pr-7",
          )}
        >
          {metaParts.map(({ key, icon: Icon, value }, index) => (
            <Fragment key={key}>
              {index > 0 && <span aria-hidden>·</span>}
              <span
                data-testid={`kanban-card-${key}`}
                className="flex max-w-full min-w-0 items-center gap-1"
              >
                {Icon && <Icon aria-hidden className="size-3.5 shrink-0" />}
                <span className="truncate">{value}</span>
              </span>
            </Fragment>
          ))}
        </div>
      )}
    </div>
  );
}
