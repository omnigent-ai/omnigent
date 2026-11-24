import { useEffect, useRef, useState } from "react";
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
import { TicketError, TicketLabels } from "./TicketControls";
import { TicketStageBadge } from "./TicketStageAppearance";
import { TicketAgentIcons } from "./TicketAgentIcons";
import { ticketPanelSearch } from "./projectHomeNavigation";
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

function collapsedStageKey(projectId: string, stageId: string) {
  return `omnigent.ticketBoard.collapsed.${projectId}.${stageId}`;
}

/** The completed stage starts collapsed; the choice is remembered per project. */
function useStageCollapsed(projectId: string | null, stage: TicketStage) {
  const read = () => {
    if (!stage.is_complete) return false;
    if (!projectId) return true;
    try {
      return window.localStorage.getItem(collapsedStageKey(projectId, stage.id)) !== "0";
    } catch {
      return true;
    }
  };
  const [collapsed, setCollapsed] = useState(read);
  useEffect(() => setCollapsed(read()), [projectId, stage.id, stage.is_complete]); // eslint-disable-line react-hooks/exhaustive-deps
  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    if (!projectId) return;
    try {
      window.localStorage.setItem(collapsedStageKey(projectId, stage.id), next ? "1" : "0");
    } catch {
      // Storage may be unavailable; the toggle still applies for this view.
    }
  };
  return [collapsed, toggle] as const;
}

export function ProjectTicketBoard({
  projectName,
  selectedTicketId,
  overlayClearance,
  ticketIds = NO_TICKET_FILTER,
}: {
  projectName: string;
  selectedTicketId: string | null;
  overlayClearance?: string;
  ticketIds?: string[];
}) {
  const { projectId, projects } = useTicketProject(projectName);
  if (projects.isLoading) return <Spinner className="m-6" aria-label="Loading project" />;
  if (projects.error)
    return (
      <div className="p-6">
        <TicketError error={projects.error} />
      </div>
    );
  return (
    <TicketBoard
      key={`${projectId}:${ticketIds.join(",")}`}
      projectId={projectId}
      projectName={projectName}
      selectedTicketId={selectedTicketId}
      overlayClearance={overlayClearance}
      ticketIds={ticketIds}
    />
  );
}

function TicketBoard({
  projectId,
  projectName,
  selectedTicketId,
  overlayClearance,
  ticketIds,
}: {
  projectId: string | null;
  projectName: string;
  selectedTicketId: string | null;
  overlayClearance?: string;
  ticketIds: string[];
}) {
  const [search] = useSearchParams();
  const [selectionLimit, setSelectionLimit] = useState(25);
  const selected = useTicketSelection(projectId, ticketIds.slice(0, selectionLimit));
  const selectedTickets = selected.flatMap((query) =>
    query.data && !query.isError ? [query.data] : [],
  );
  const selectionPending = selected.some((query) => query.isPending);
  const unfilteredSearch = new URLSearchParams(search);
  unfilteredSearch.delete("tickets");
  const [dragged, setDragged] = useState<ProjectTicket | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const lifecycle = useTicketLifecycleEnabled();
  const configuration = useTicketStages(projectId);
  const stages = configuration.data?.stages ?? [];
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
          <TicketError error={selected.find((query) => query.isError)?.error ?? null} />
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
      <TicketError error={update.error} />
      {configuration.isPending && <Spinner className="m-6" aria-label="Loading stages" />}
      <TicketError error={configuration.error} />
      {configuration.isError && (
        <Button variant="ghost" onClick={() => void configuration.refetch()}>
          Try again
        </Button>
      )}
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
}: {
  projectId: string | null;
  stage: TicketStage;
  initial: boolean;
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
}) {
  const [search] = useSearchParams();
  const query = useTickets(projectId, stage.id, filteredTickets === undefined);
  const tickets = filteredTickets ?? query.data?.pages.flatMap((page) => page.data) ?? [];
  const hasNextPage = filteredTickets === undefined && query.hasNextPage;
  const { setNodeRef, isOver } = useDroppable({ id: stage.id, disabled });
  const [collapsedPreference, toggleCollapsed] = useStageCollapsed(projectId, stage);
  const collapsed = collapsedPreference && !filteredTickets?.length;
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
        <span className="text-sm font-medium text-muted-foreground tabular-nums">
          {tickets.length}
          {hasNextPage ? "+" : ""}
        </span>
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
        "flex min-w-60 flex-1 basis-0 flex-col rounded-xl border border-transparent bg-muted/30 px-2 pt-2 transition-colors",
        dragging && "border-dashed border-muted-foreground/50",
        dragging && isOver && "border-primary bg-primary/10",
      )}
    >
      <h2 className="flex shrink-0 items-center justify-between px-1.5 pt-1.5 pb-3 text-sm font-medium text-muted-foreground">
        <TicketStageBadge stage={stage} showInstructionsOnHover />
        <span className="flex items-center gap-1 tabular-nums">
          {tickets.length}
          {hasNextPage ? "+" : ""}
          {stage.is_complete && (
            <Button
              variant="ghost"
              size="icon"
              className="size-6"
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
        <TicketError error={filteredTickets === undefined ? query.error : null} />
        {filteredTickets === undefined && query.isError && (
          <Button variant="ghost" onClick={() => void query.refetch()}>
            Try again
          </Button>
        )}
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
          (filteredTickets !== undefined || (!query.isError && (!projectId || query.isSuccess))) &&
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
  return (
    <>
      <div className="flex items-start gap-2">
        <h3 className="min-w-0 flex-1 text-ui leading-snug font-medium wrap-anywhere">
          {ticket.title}
        </h3>
        {ticket.has_running_agents === true && (
          <span role="img" aria-label="Running" className="mt-0.5 shrink-0">
            <RunningDot />
          </span>
        )}
      </div>
      {ticket.labels.length > 0 && <TicketLabels labels={ticket.labels} compact />}
      {ticket.latest_update &&
        (ticket.latest_update !== "Created the ticket" || ticket.comment_count > 0) && (
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
        <div className="flex shrink-0 items-center gap-2">
          <TicketNewActivity ticket={ticket} />
          {ticket.comment_count > 0 && (
            <span
              className="flex items-center gap-1.5 tabular-nums"
              aria-label={`${ticket.comment_count} comments`}
            >
              <MessageSquareIcon aria-hidden className="size-3.5" />
              {ticket.comment_count}
            </span>
          )}
        </div>
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
    <span className="min-w-0 truncate" title={`Requested by ${label}`} data-testid="ticket-requester">
      by {label}
    </span>
  );
}

/** Comments meant for the viewer since they last opened the ticket, like the sidebar Inbox badge. */
function TicketNewActivity({ ticket }: { ticket: ProjectTicket }) {
  const comments = ticket.new_comment_count ?? 0;
  if (comments === 0) return null;
  const label = `${comments} new ${comments === 1 ? "comment" : "comments"}`;
  return (
    <span
      data-testid="ticket-new-activity"
      title={label}
      aria-label={label}
      className="rounded-full bg-primary px-1.5 text-xs leading-5 font-semibold text-primary-foreground tabular-nums"
    >
      +{comments}
    </span>
  );
}
