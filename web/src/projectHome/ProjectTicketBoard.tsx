import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
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
import {
  CircleAlertIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  MessageSquareIcon,
  PlusIcon,
  XIcon,
} from "lucide-react";
import { RunningDot } from "@/components/RunningDot";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useIsPhoneViewport } from "@/hooks/useIsPhoneViewport";
import { useNow } from "@/hooks/useNow";
import { Link, useSearchParams } from "@/lib/routing";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { getCurrentUserId } from "@/lib/identity";
import type { ProjectTicket, TicketStage } from "@/lib/projectTicketsApi";
import { personLabel, RequesterName } from "./TicketActor";
import { markdownPlainText } from "@/lib/markdownPlainText";
import { cn } from "@/lib/utils";
import { shownLoadError, TicketError, TicketLabels, TicketLoadError } from "./TicketControls";
import { TicketShipBlocker } from "./TicketShipBlocker";
import { TicketStageBadge } from "./TicketStageAppearance";
import { TicketAgentIcons, TicketAgentIconsSpace } from "./TicketAgentIcons";
import { OttoDoneMark, TidePoolScene } from "@/components/TidePool";
import { usePlayfulTouches } from "@/lib/playfulTouchesPreferences";
import { TicketBoardRequesterFilter, useBoardRequesterFilter } from "./TicketBoardRequesterFilter";
import { NEEDS_YOU_PARAM } from "./needsYouFilter";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { needsWhom } from "./ticketWaitingOn";
import { collapsedLanes, useContentWidth, useLanePreferences } from "./boardLaneLayout";
import {
  useRelatedTickets,
  useTicketActions,
  useTicketLifecycleEnabled,
  useTicketProject,
  useTickets,
  useTicketStages,
  useTicketSelection,
} from "./useProjectTickets";

const NO_TICKET_FILTER: string[] = [];
const NOT_NEW = () => false;
// Whether a card is new since the viewer last opened the board (see useNewTicketsOnOpen).
const NewTicketContext = createContext<(ticket: ProjectTicket) => boolean>(NOT_NEW);

export function ProjectTicketBoard({
  projectName,
  selectedTicketId,
  overlayClearance,
  ticketIds = NO_TICKET_FILTER,
  isNewTicket = NOT_NEW,
  requesterFilterInHeader = false,
}: {
  /** The page shows the All tickets / Yours filter in its tab row, so the board doesn't. */
  requesterFilterInHeader?: boolean;
  projectName: string;
  selectedTicketId: string | null;
  overlayClearance?: string;
  ticketIds?: string[];
  isNewTicket?: (ticket: ProjectTicket) => boolean;
}) {
  const { projectId, projects } = useTicketProject(projectName);
  if (projects.isLoading) return <Spinner className="m-6" aria-label="Loading project" />;
  if (projects.error)
    return (
      <div className="px-4 pt-3">
        <TicketLoadError error={projects.error} />
      </div>
    );
  return (
    <NewTicketContext.Provider value={isNewTicket}>
      <TicketBoard
        key={`${projectId}:${ticketIds.join(",")}`}
        projectId={projectId}
        projectName={projectName}
        selectedTicketId={selectedTicketId}
        overlayClearance={overlayClearance}
        ticketIds={ticketIds}
        requesterFilterInHeader={requesterFilterInHeader}
      />
    </NewTicketContext.Provider>
  );
}

function TicketBoard({
  projectId,
  projectName,
  selectedTicketId,
  overlayClearance,
  ticketIds,
  requesterFilterInHeader,
}: {
  projectId: string | null;
  projectName: string;
  selectedTicketId: string | null;
  overlayClearance?: string;
  ticketIds: string[];
  requesterFilterInHeader: boolean;
}) {
  const [search] = useSearchParams();
  const [selectionLimit, setSelectionLimit] = useState(25);
  const selected = useTicketSelection(projectId, ticketIds.slice(0, selectionLimit));
  // A restart keeps the tickets already shown; another failure drops them.
  const selectionErrors = selected.map((query) =>
    shownLoadError(query.error, query.data !== undefined),
  );
  const selectedTickets = selected.flatMap((query, index) =>
    query.data && !selectionErrors[index] ? [query.data] : [],
  );
  const selectionPending = selected.some((query) => query.isPending);
  const unfilteredSearch = new URLSearchParams(search);
  unfilteredSearch.delete("tickets");
  unfilteredSearch.delete(NEEDS_YOU_PARAM);
  const [mine, setMine] = useBoardRequesterFilter(projectId);
  const [dragged, setDragged] = useState<ProjectTicket | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const lifecycle = useTicketLifecycleEnabled();
  const configuration = useTicketStages(projectId);
  // Each lane reports how many tickets it holds, from the query it already runs; a project
  // whose lanes are all empty gets the first-job welcome. No extra request.
  const [laneCounts, setLaneCounts] = useState<Record<string, number | null>>({});
  const reportLaneCount = useCallback(
    (stageId: string, count: number | null) =>
      setLaneCounts((current) => (current[stageId] === count ? current : { ...current, [stageId]: count })),
    [],
  );
  // Phones show one lane at a time, picked from tabs that carry each lane's shown count.
  const isPhone = useIsPhoneViewport();
  const [shownCounts, setShownCounts] = useState<Record<string, string>>({});
  const reportShownCount = useCallback(
    (stageId: string, count: string) =>
      setShownCounts((current) =>
        current[stageId] === count ? current : { ...current, [stageId]: count },
      ),
    [],
  );
  const [chosenLane, setChosenLane] = useState<string | null>(null);
  const stageIds = configuration.data?.stages.map((stage) => stage.id) ?? [];
  const projectEmpty = stageIds.length > 0 && stageIds.every((id) => laneCounts[id] === 0);
  const stages = configuration.data?.stages ?? [];
  // Until every lane has a count, the first stage; then the first lane with tickets.
  const allCounted = stageIds.length > 0 && stageIds.every((id) => id in shownCounts);
  const phoneLane =
    (chosenLane !== null && stageIds.includes(chosenLane) ? chosenLane : null) ??
    (allCounted ? stageIds.find((id) => parseInt(shownCounts[id], 10) > 0) : undefined) ??
    configuration.data?.initial_stage_id ??
    stageIds[0];
  const boardRef = useRef<HTMLDivElement>(null);
  const boardWidth = useContentWidth(boardRef);
  const [lanePreferences, setLaneCollapsed] = useLanePreferences(projectId);
  const collapsed = collapsedLanes(
    stages,
    lanePreferences,
    configuration.data?.initial_stage_id,
    boardWidth,
  );
  const related = useRelatedTickets(projectId, lifecycle ? (hoveredId ?? focusedId) : null);
  const { hasNextPage, isFetching, isError, fetchNextPage } = related;
  const relatedIds = new Set(
    related.data?.pages.flatMap((page) => page.data.map((ticket) => ticket.id)),
  );
  useEffect(() => {
    if (hasNextPage && !isFetching && !isError) void fetchNextPage();
  }, [hasNextPage, isFetching, isError, fetchNextPage]);
  const dragEnded = useRef(0);
  const { update } = useTicketActions(projectId ?? "");
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      {ticketIds.length > 0 && (
        <div className="flex shrink-0 items-center gap-2 px-4 pb-3 text-sm text-muted-foreground">
          <span>
            {ticketIds.length} {ticketIds.length === 1 ? "ticket" : "tickets"}
          </span>
          <Button asChild variant="ghost" size="icon-xs">
            <Link to={`?${unfilteredSearch}`} aria-label="Clear ticket filter">
              <XIcon />
            </Link>
          </Button>
          {selectionPending && (
            <Spinner aria-label="Loading selected tickets" className="size-3.5" />
          )}
          <TicketError error={selectionErrors.find(Boolean) ?? null} />
          {selectionLimit < ticketIds.length && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSelectionLimit((limit) => limit + 25)}
            >
              Load more
            </Button>
          )}
        </div>
      )}
      {ticketIds.length === 0 && !requesterFilterInHeader && (
        <div className="flex shrink-0 items-center px-4 pb-2">
          <TicketBoardRequesterFilter mine={mine} onChange={setMine} />
        </div>
      )}
      {(update.error || shownLoadError(configuration.error, configuration.data !== undefined)) && (
        <div className="flex shrink-0 flex-col gap-2 px-4 pt-3 pb-2">
          <TicketLoadError error={update.error} action />
          <TicketLoadError
            error={configuration.error}
            hasData={configuration.data !== undefined}
            onRetry={() => void configuration.refetch()}
          />
        </div>
      )}
      {configuration.isPending && <Spinner className="m-6" aria-label="Loading stages" />}
      <DndContext
        sensors={sensors}
        onDragStart={({ active }) => setDragged(active.data.current?.ticket ?? null)}
        onDragCancel={() => {
          setDragged(null);
          dragEnded.current = Date.now();
        }}
        onDragEnd={({ over }) => {
          const ticket = dragged;
          setDragged(null);
          dragEnded.current = Date.now();
          const stage = stages.find((candidate) => candidate.id === over?.id);
          if (!ticket || !stage || ticket.stage_id === stage.id) return;
          update.reset();
          update.mutate({ id: ticket.id, changes: { stage_id: stage.id } });
        }}
      >
        {isPhone && stages.length > 0 && (
          <PhoneLaneTabs
            stages={stages}
            lane={phoneLane}
            counts={shownCounts}
            onLaneChange={setChosenLane}
          />
        )}
        <div
          ref={boardRef}
          role="region"
          aria-label={`${projectName} ticket board`}
          data-testid="project-ticket-board"
          className="flex min-h-0 flex-1 gap-4 overflow-x-auto px-4 pt-1 pb-4"
        >
          {stages.map((stage) => (
            <TicketColumn
              key={stage.id}
              stage={stage}
              initial={stage.id === configuration.data?.initial_stage_id}
              collapsedByLayout={!isPhone && collapsed.has(stage.id)}
              phone={isPhone ? { shown: stage.id === phoneLane } : undefined}
              onShownCountChange={isPhone ? reportShownCount : undefined}
              onCollapsedChange={(next) => setLaneCollapsed(stage.id, next)}
              projectId={projectId}
              selectedTicketId={selectedTicketId}
              dragging={dragged !== null}
              disabled={update.isPending}
              overlayClearance={overlayClearance}
              suppressClick={() => Date.now() - dragEnded.current < 250}
              relatedIds={relatedIds}
              onHover={setHoveredId}
              onFocus={setFocusedId}
              filteredTickets={
                ticketIds.length
                  ? selectedTickets.filter((ticket) => ticket.stage_id === stage.id)
                  : undefined
              }
              selectionPending={selectionPending}
              mine={mine}
              projectEmpty={projectEmpty}
              onCountChange={reportLaneCount}
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {dragged && (
            <div className="relative">
              <div className="flex flex-col gap-3 rounded-xl border bg-card p-3.5 shadow-lg">
                <TicketCardContent ticket={dragged} />
              </div>
              <TicketAgentIcons ticket={dragged} className="absolute right-3.5 bottom-3" />
            </div>
          )}
        </DragOverlay>
      </DndContext>
    </div>
  );
}

// Keeps the selected tab clear of the strip's edge when scrolled into view.
const LANE_TAB_EDGE_PX = 16;

/** The phone board's stage switcher: one underline tab per stage, with its count. */
function PhoneLaneTabs({
  stages,
  lane,
  counts,
  onLaneChange,
}: {
  stages: TicketStage[];
  lane: string | undefined;
  counts: Record<string, string>;
  onLaneChange: (lane: string) => void;
}) {
  const stripRef = useRef<HTMLDivElement>(null);
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
    <Tabs value={lane} onValueChange={onLaneChange} className="shrink-0 gap-0">
      <div
        ref={stripRef}
        className="no-scrollbar mx-4 mb-3 shrink-0 overflow-x-auto overflow-y-hidden border-b border-border"
      >
        <TabsList
          variant="line"
          aria-label="Stages"
          className="h-10 w-max min-w-full justify-start gap-3.5 rounded-none p-0"
        >
          {stages.map((stage) => (
            <TabsTrigger
              key={stage.id}
              value={stage.id}
              data-testid={`ticket-lane-tab-${stage.id}`}
              className="h-full flex-none gap-1 rounded-none border-0 border-b-2 border-transparent px-0 after:hidden focus-visible:border-foreground focus-visible:ring-0 focus-visible:outline-none data-[state=active]:border-foreground data-[state=active]:text-foreground dark:data-[state=active]:text-foreground"
            >
              {stage.name}
              {counts[stage.id] !== undefined && (
                <Badge className="border-transparent bg-muted text-muted-foreground tabular-nums">
                  {counts[stage.id]}
                </Badge>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
    </Tabs>
  );
}

function TicketColumn({
  projectId,
  stage,
  initial,
  collapsedByLayout,
  onCollapsedChange,
  selectedTicketId,
  dragging,
  disabled,
  suppressClick,
  overlayClearance,
  relatedIds,
  onHover,
  onFocus,
  filteredTickets,
  selectionPending,
  mine,
  projectEmpty = false,
  onCountChange,
  phone,
  onShownCountChange,
}: {
  projectId: string | null;
  stage: TicketStage;
  initial: boolean;
  /** Collapsed to a strip, by the person's choice or because the board ran out of width. */
  collapsedByLayout: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
  selectedTicketId: string | null;
  dragging: boolean;
  disabled: boolean;
  suppressClick: () => boolean;
  overlayClearance?: string;
  relatedIds: Set<string>;
  onHover: (id: string | null) => void;
  onFocus: (id: string | null) => void;
  filteredTickets?: ProjectTicket[];
  selectionPending: boolean;
  /** Only the viewer's tickets. */
  mine: boolean;
  /** The project has no tickets at all. */
  projectEmpty?: boolean;
  /** This lane's ticket count, or null while unknown or filtered. */
  onCountChange?: (stageId: string, count: number | null) => void;
  /** Phones: one lane fills the board, named by its tab; the others stay loaded but hidden. */
  phone?: { shown: boolean };
  /** The count the lane shows, once loaded; phones put it on the lane's tab. */
  onShownCountChange?: (stageId: string, count: string) => void;
}) {
  const [search] = useSearchParams();
  const playful = usePlayfulTouches();
  const query = useTickets(projectId, stage.id, filteredTickets === undefined, mine);
  const tickets = filteredTickets ?? query.data?.pages.flatMap((page) => page.data) ?? [];
  const hasNextPage = filteredTickets === undefined && query.hasNextPage;
  const laneCount =
    filteredTickets !== undefined || mine || query.data === undefined
      ? null
      : (query.data.pages[0]?.total ?? (hasNextPage ? null : tickets.length));
  useEffect(() => onCountChange?.(stage.id, laneCount), [onCountChange, stage.id, laneCount]);
  // The server's total for the lane; older servers only report what has loaded, plus "+".
  const total = filteredTickets === undefined ? query.data?.pages[0]?.total : undefined;
  const count = total ?? `${tickets.length}${hasNextPage ? "+" : ""}`;
  const countKnown = filteredTickets !== undefined || query.data !== undefined;
  useEffect(() => {
    if (countKnown) onShownCountChange?.(stage.id, String(count));
  }, [onShownCountChange, stage.id, count, countKnown]);
  const { setNodeRef, isOver } = useDroppable({ id: stage.id, disabled });
  // A filtered board opens every lane that has a match.
  const collapsed = collapsedByLayout && !filteredTickets?.length;
  const toggleCollapsed = () => onCollapsedChange(!collapsed);
  const loadError =
    filteredTickets === undefined && query.isError
      ? shownLoadError(query.error, query.data !== undefined)
      : null;
  if (collapsed) {
    return (
      <section
        ref={setNodeRef}
        aria-label={stage.name}
        className={cn(
          "flex w-12 shrink-0 flex-col items-center gap-3 rounded-xl border border-transparent bg-muted/30 py-2 transition-colors",
          dragging && "border-dashed border-muted-foreground/50",
          dragging && isOver && "border-primary bg-primary/10",
        )}
      >
        <Button
          variant="ghost"
          size="icon"
          aria-label={`Expand ${stage.name}`}
          title={`Expand ${stage.name}`}
          onClick={toggleCollapsed}
        >
          <ChevronLeftIcon />
        </Button>
        {loadError ? (
          // A count would make a failed load look like an empty column, so show the error.
          <Button
            variant="ghost"
            size="icon"
            className="text-destructive hover:text-destructive"
            aria-label={`Couldn't load ${stage.name} tickets: ${loadError.message}. Try again`}
            title={`Couldn't load tickets: ${loadError.message}. Click to try again.`}
            loading={query.isFetching}
            onClick={() => void query.refetch()}
          >
            <CircleAlertIcon />
          </Button>
        ) : (
          <span className="text-sm font-medium text-muted-foreground tabular-nums">{count}</span>
        )}
        <button
          type="button"
          onClick={toggleCollapsed}
          className="text-sm font-medium text-muted-foreground [writing-mode:vertical-rl]"
        >
          {stage.name}
        </button>
      </section>
    );
  }
  return (
    <section
      ref={setNodeRef}
      aria-label={stage.name}
      hidden={phone !== undefined && !phone.shown}
      className={cn(
        "group/lane flex min-w-52 flex-1 basis-0 flex-col rounded-xl border border-transparent bg-muted/30 px-2 pt-2 transition-colors",
        dragging && "border-dashed border-muted-foreground/50",
        dragging && isOver && "border-primary bg-primary/10",
        phone && !phone.shown && "hidden",
      )}
    >
      <h2
        className={cn(
          "flex shrink-0 items-center justify-between px-1.5 pt-1.5 pb-3 text-sm font-medium text-muted-foreground",
          phone && "sr-only",
        )}
      >
        <TicketStageBadge stage={stage} showInstructionsOnHover />
        {/* Pinned to the badge's height so a complete stage's collapse button can't lower its row. */}
        <span className="flex h-5 items-center gap-1 tabular-nums">
          {count}
          {!filteredTickets && !phone && (
            <Button
              variant="ghost"
              size="icon"
              className={cn(
                "size-6",
                !stage.is_complete &&
                  "opacity-0 group-hover/lane:opacity-100 focus-visible:opacity-100",
              )}
              aria-label={`Collapse ${stage.name}`}
              title={`Collapse ${stage.name}`}
              onClick={toggleCollapsed}
            >
              <ChevronRightIcon />
            </Button>
          )}
        </span>
      </h2>
      <div
        className="-mx-2 flex min-h-0 flex-1 flex-col gap-3 overflow-x-hidden overflow-y-auto px-2 pt-0.5 pb-2"
        style={{ paddingBottom: overlayClearance }}
      >
        {projectId && filteredTickets === undefined && query.isPending ? (
          <Spinner aria-label="Loading tickets" className="mx-auto my-4" />
        ) : null}
        <TicketLoadError
          error={filteredTickets === undefined ? query.error : null}
          hasData={query.data !== undefined}
          onRetry={() => void query.refetch()}
        />
        {tickets.map((ticket) => (
          <TicketCard
            key={ticket.id}
            ticket={ticket}
            selected={ticket.id === selectedTicketId}
            disabled={disabled}
            suppressClick={suppressClick}
            related={relatedIds.has(ticket.id)}
            onHover={onHover}
            onFocus={onFocus}
            finished={stage.is_complete}
          />
        ))}
        {!selectionPending &&
          (filteredTickets !== undefined ||
            (!loadError && (!projectId || query.data !== undefined))) &&
          tickets.length === 0 &&
          (playful && initial && projectEmpty && filteredTickets === undefined && !mine ? (
            <NewProjectWelcome />
          ) : (
            <p className="px-2 py-4 text-sm text-muted-foreground">
              {playful ? emptyLaneLine(stage, initial) : "No tickets"}
            </p>
          ))}
        {hasNextPage && (
          <Button
            variant="ghost"
            loading={query.isFetchingNextPage}
            onClick={() => void query.fetchNextPage()}
          >
            Load more
          </Button>
        )}
        {initial && (
          <Button variant="ghost" asChild className="justify-start text-muted-foreground">
            <Link to={ticketPanelSearch("new", search)}>
              <PlusIcon />
              New ticket
            </Link>
          </Button>
        )}
      </div>
    </section>
  );
}

function TicketCard({
  ticket,
  selected,
  disabled,
  suppressClick,
  related,
  onHover,
  onFocus,
  finished = false,
}: {
  ticket: ProjectTicket;
  selected: boolean;
  disabled: boolean;
  suppressClick: () => boolean;
  related: boolean;
  onHover: (id: string | null) => void;
  onFocus: (id: string | null) => void;
  /** In the project's completed stage. */
  finished?: boolean;
}) {
  const [search] = useSearchParams();
  const { setNodeRef, listeners, attributes, isDragging } = useDraggable({
    id: ticket.id,
    data: { ticket },
    disabled,
  });
  return (
    <div className="relative">
      <Link
        ref={setNodeRef}
        {...attributes}
        {...listeners}
        role="link"
        to={ticketPanelSearch(ticket.id, search)}
        aria-label={`Open ticket: ${ticket.title}`}
        data-related-highlight={related || undefined}
        onMouseEnter={() => onHover(ticket.id)}
        onMouseLeave={() => onHover(null)}
        onFocus={() => onFocus(ticket.id)}
        onBlur={() => onFocus(null)}
        onClick={(event) => {
          if (suppressClick()) event.preventDefault();
        }}
        className={cn(
          "flex min-w-0 flex-col gap-3 rounded-xl border border-border/60 bg-card p-3.5 text-left shadow-xs outline-none transition-colors hover:border-primary/70 hover:bg-primary/5 hover:ring-2 hover:ring-primary/20 focus-visible:bg-primary/5 focus-visible:ring-2 focus-visible:ring-ring",
          related && "border-muted-foreground/50",
          // Cards waiting on the viewer stand out in a lane, until they answer.
          needsWhom(ticket.waiting_on, getCurrentUserId())?.kind === "you" &&
            // Important: dark mode's glass-card rule resets every card border.
            "border-l-4! border-l-brand-accent!",
          selected && "border-primary ring-1 ring-primary/20",
          isDragging && "opacity-40",
        )}
      >
        <TicketCardContent ticket={ticket} finished={finished} />
      </Link>
      {/* Over the card's footer corner, outside the link so each icon stays its own control. */}
      <TicketAgentIcons ticket={ticket} className="absolute right-3.5 bottom-3" />
    </div>
  );
}

function TicketCardContent({ ticket, finished = false }: { ticket: ProjectTicket; finished?: boolean }) {
  const now = useNow();
  const playful = usePlayfulTouches();
  const showDone = playful && finished && isRecentlyFinishedForViewer(ticket, now.getTime());
  const isNew = useContext(NewTicketContext)(ticket);
  return (
    <>
      <div className="flex items-start gap-2">
        <h3 className="min-w-0 flex-1 text-ui leading-snug font-medium wrap-anywhere">
          {ticket.title}
        </h3>
        {isNew && (
          <span
            data-testid="ticket-new"
            title="New since you last opened this board"
            className="mt-0.5 shrink-0 rounded-full bg-primary-cta px-1.5 text-xs leading-5 font-semibold text-primary-cta-foreground"
          >
            New
          </span>
        )}
        {ticket.has_running_agents === true && (
          <span role="img" aria-label="Running" className="mt-0.5 shrink-0">
            <RunningDot />
          </span>
        )}
      </div>
      <TicketShipBlocker projectId={ticket.project_id} ticketId={ticket.id} variant="card" />
      {ticket.labels.length > 0 && (
        <TicketLabels labels={ticket.labels} compact waitingOn={ticket.waiting_on} />
      )}
      <TicketStatusLine ticket={ticket} />
      <div className="flex items-center justify-between gap-2 text-sm text-muted-foreground">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <time
            dateTime={new Date(ticket.updated_at * 1000).toISOString()}
            title={`Updated ${absoluteTime(ticket.updated_at * 1000)}`}
          >
            {relativeTime(ticket.updated_at * 1000, now.getTime())}
          </time>
          <TicketRequester ticket={ticket} />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {showDone && <OttoDoneMark />}
          <TicketCommentCount ticket={ticket} />
          {/* Room for the agent icons drawn over this corner. */}
          <TicketAgentIconsSpace ticket={ticket} />
        </div>
      </div>
    </>
  );
}

/**
 * The card's second line: the newest comment for the human, so a later move or label
 * edit doesn't hide it. Without one (older servers, or one past the server's scan), the
 * last update shows unless it is an event line.
 */
function TicketStatusLine({ ticket }: { ticket: ProjectTicket }) {
  const comment = ticket.latest_comment;
  const source =
    comment?.text ||
    (ticket.latest_update && !isSystemUpdate(ticket.latest_update, ticket.comment_count)
      ? ticket.latest_update
      : null);
  // A two-line preview: Markdown marks would show as literal characters.
  const text = source ? markdownPlainText(source) : null;
  if (!text) return null;
  const author = comment
    ? comment.by_person && comment.user_id
      ? personLabel(comment.user_id, getCurrentUserId())
      : "an agent"
    : null;
  return (
    <p
      data-testid="ticket-status-line"
      title={
        comment
          ? `Latest comment${author ? ` by ${author}` : ""}, ${absoluteTime(comment.created_at * 1000)}`
          : undefined
      }
      className="line-clamp-2 text-sm leading-relaxed text-muted-foreground wrap-anywhere"
    >
      {text}
    </p>
  );
}

/** Who asked for a ticket, when it wasn't the viewer: a shared member or an outside PR author. */
function TicketRequester({ ticket }: { ticket: ProjectTicket }) {
  const viewer = getCurrentUserId();
  if (!ticket.requested_by || ticket.requested_by === viewer) return null;
  return (
    <span className="flex min-w-0 items-center" data-testid="ticket-requester">
      <RequesterName requestedBy={ticket.requested_by} viewer={viewer} avatar />
    </span>
  );
}

/** How long a finished ticket you asked for keeps Otto's Done mark. */
export const DONE_MARK_MS = 24 * 60 * 60 * 1000;

/** A ticket the viewer asked for that reached the completed stage within the last day. */
export function isRecentlyFinishedForViewer(ticket: ProjectTicket, nowMs: number): boolean {
  const viewer = getCurrentUserId();
  if (!viewer || ticket.requested_by !== viewer) return false;
  return nowMs - ticket.updated_at * 1000 < DONE_MARK_MS;
}

/** A neutral line for an empty lane; it says where the lane sits, never that work is late. */
export function emptyLaneLine(stage: TicketStage, initial: boolean): string {
  if (stage.is_complete) return "Nothing finished yet.";
  if (initial) return "Nothing new right now.";
  return `Nothing in ${stage.name} right now.`;
}

function NewProjectWelcome() {
  return (
    <div
      className="flex flex-col items-center gap-2 px-2 pt-4 pb-2 text-center"
      data-testid="new-project-welcome"
    >
      <TidePoolScene className="w-44" />
      <p className="font-semibold text-foreground">Your factory is ready</p>
      <p className="max-w-56 text-sm text-muted-foreground">
        Describe the first job, and the factory takes it from there.
      </p>
    </div>
  );
}

/**
 * Event lines the server records for moves, label edits and links. The lane already shows
 * the stage, so a card keeps its last real update rather than repeating them.
 */
const SYSTEM_UPDATE = /^(Updated the ticket|Moved to .+|Related to .+|Unlinked .+)$/;

export function isSystemUpdate(update: string, commentCount: number): boolean {
  // A comment replaces the creation line, so with comments it can only be a comment's text.
  if (update === "Created the ticket") return commentCount === 0;
  return SYSTEM_UPDATE.test(update);
}

/**
 * The comment count, in the accent with a dot when some are new to the viewer since they
 * last opened the ticket (like the sidebar Inbox badge).
 */
function TicketCommentCount({ ticket }: { ticket: ProjectTicket }) {
  const total = ticket.comment_count;
  const fresh = Math.min(ticket.new_comment_count ?? 0, total);
  if (total === 0) return null;
  const label =
    fresh > 0
      ? `${total} ${total === 1 ? "comment" : "comments"}, ${fresh} new`
      : `${total} ${total === 1 ? "comment" : "comments"}`;
  return (
    <span
      data-testid={fresh > 0 ? "ticket-new-activity" : undefined}
      title={fresh > 0 ? `${fresh} new since you last opened this ticket` : undefined}
      aria-label={label}
      className={cn(
        "flex shrink-0 items-center gap-1.5 tabular-nums",
        fresh > 0 && "font-semibold text-brand-accent",
      )}
    >
      <span className="relative">
        <MessageSquareIcon aria-hidden className="size-3.5" />
        {fresh > 0 && (
          <span
            aria-hidden
            className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-brand-accent ring-2 ring-card"
          />
        )}
      </span>
      {total}
    </span>
  );
}
