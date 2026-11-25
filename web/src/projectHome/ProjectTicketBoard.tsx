import { createContext, useContext, useEffect, useRef, useState } from "react";
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
import { Spinner } from "@/components/ui/spinner";
import { useNow } from "@/hooks/useNow";
import { Link, useSearchParams } from "@/lib/routing";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { getCurrentUserId } from "@/lib/identity";
import { requesterLabel, type ProjectTicket, type TicketStage } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
import { shownLoadError, TicketError, TicketLabels, TicketLoadError } from "./TicketControls";
import { TicketShipBlocker } from "./TicketShipBlocker";
import { TicketStageBadge } from "./TicketStageAppearance";
import { TicketAgentIcons } from "./TicketAgentIcons";
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
  const stages = configuration.data?.stages ?? [];
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
              collapsedByLayout={collapsed.has(stage.id)}
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
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {dragged && (
            <div>
              <div className="flex flex-col gap-3 rounded-xl border bg-card p-3.5 shadow-lg">
                <TicketCardContent ticket={dragged} />
              </div>
              <TicketAgentIcons ticket={dragged} />
            </div>
          )}
        </DragOverlay>
      </DndContext>
    </div>
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
}) {
  const [search] = useSearchParams();
  const query = useTickets(projectId, stage.id, filteredTickets === undefined, mine);
  const tickets = filteredTickets ?? query.data?.pages.flatMap((page) => page.data) ?? [];
  const hasNextPage = filteredTickets === undefined && query.hasNextPage;
  // The server's total for the lane; older servers only report what has loaded, plus "+".
  const total = filteredTickets === undefined ? query.data?.pages[0]?.total : undefined;
  const count = total ?? `${tickets.length}${hasNextPage ? "+" : ""}`;
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
      className={cn(
        "group/lane flex min-w-52 flex-1 basis-0 flex-col rounded-xl border border-transparent bg-muted/30 px-2 pt-2 transition-colors",
        dragging && "border-dashed border-muted-foreground/50",
        dragging && isOver && "border-primary bg-primary/10",
      )}
    >
      <h2 className="flex shrink-0 items-center justify-between px-1.5 pt-1.5 pb-3 text-sm font-medium text-muted-foreground">
        <TicketStageBadge stage={stage} showInstructionsOnHover />
        {/* Pinned to the badge's height so a complete stage's collapse button can't lower its row. */}
        <span className="flex h-5 items-center gap-1 tabular-nums">
          {count}
          {!filteredTickets && (
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
          />
        ))}
        {!selectionPending &&
          (filteredTickets !== undefined ||
            (!loadError && (!projectId || query.data !== undefined))) &&
          tickets.length === 0 && (
            <p className="px-2 py-4 text-sm text-muted-foreground">No tickets</p>
          )}
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
}: {
  ticket: ProjectTicket;
  selected: boolean;
  disabled: boolean;
  suppressClick: () => boolean;
  related: boolean;
  onHover: (id: string | null) => void;
  onFocus: (id: string | null) => void;
}) {
  const [search] = useSearchParams();
  const { setNodeRef, listeners, attributes, isDragging } = useDraggable({
    id: ticket.id,
    data: { ticket },
    disabled,
  });
  return (
    <div>
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
        <TicketCardContent ticket={ticket} />
      </Link>
      <TicketAgentIcons ticket={ticket} />
    </div>
  );
}

function TicketCardContent({ ticket }: { ticket: ProjectTicket }) {
  const now = useNow();
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
      {ticket.latest_update && !isSystemUpdate(ticket.latest_update, ticket.comment_count) && (
        <p className="line-clamp-2 text-sm leading-relaxed text-muted-foreground wrap-anywhere">
          {ticket.latest_update}
        </p>
      )}
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
        <TicketCommentCount ticket={ticket} />
      </div>
    </>
  );
}

/** Who asked for a ticket, when it wasn't the viewer: a shared member or an outside PR author. */
function TicketRequester({ ticket }: { ticket: ProjectTicket }) {
  const viewer = getCurrentUserId();
  if (!ticket.requested_by || ticket.requested_by === viewer) return null;
  const label = requesterLabel(ticket.requested_by, viewer);
  return (
    <span
      className="min-w-0 truncate"
      title={`Requested by ${label}`}
      data-testid="ticket-requester"
    >
      by {label}
    </span>
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
