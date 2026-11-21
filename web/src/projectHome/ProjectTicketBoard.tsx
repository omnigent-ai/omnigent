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
import { MessageSquareIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useNow } from "@/hooks/useNow";
import { Link } from "@/lib/routing";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ProjectTicket, TicketStage } from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
import {
  CompleteTicketDialog,
  ReviewTicketDialog,
  TicketError,
  TicketTags,
} from "./TicketControls";
import { ProjectTicketSources } from "./ProjectTicketSources";
import { TicketStageBadge } from "./TicketStageAppearance";
import {
  useRelatedTickets,
  useTicketActions,
  useTicketLifecycleEnabled,
  useTicketProject,
  useTickets,
  useTicketStages,
} from "./useProjectTickets";

export function ProjectTicketBoard({
  projectName,
  selectedTicketId,
  overlayClearance,
}: {
  projectName: string;
  selectedTicketId: string | null;
  overlayClearance?: string;
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
      projectId={projectId}
      projectName={projectName}
      selectedTicketId={selectedTicketId}
      overlayClearance={overlayClearance}
    />
  );
}

function TicketBoard({
  projectId,
  projectName,
  selectedTicketId,
  overlayClearance,
}: {
  projectId: string | null;
  projectName: string;
  selectedTicketId: string | null;
  overlayClearance?: string;
}) {
  const [dragged, setDragged] = useState<ProjectTicket | null>(null);
  const [completing, setCompleting] = useState<{ ticket: ProjectTicket; stageId: string } | null>(
    null,
  );
  const [reviewing, setReviewing] = useState<ProjectTicket | null>(null);
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
      <ProjectTicketSources key={projectName} projectId={projectId} projectName={projectName} />
      <TicketError error={completing || reviewing ? null : update.error} />
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
          if (!configuration.supported && stage.is_complete)
            setCompleting({ ticket, stageId: stage.id });
          else if (!configuration.supported && stage.id === "review") setReviewing(ticket);
          else update.mutate({ id: ticket.id, changes: { stage_id: stage.id } });
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
            />
          ))}
        </div>
        <DragOverlay dropAnimation={null}>
          {dragged && (
            <div className="rounded-xl border bg-background p-3 shadow-lg">
              <TicketCardContent ticket={dragged} />
            </div>
          )}
        </DragOverlay>
      </DndContext>
      {completing && (
        <CompleteTicketDialog
          title={completing.ticket.title}
          stages={stages}
          initialStageId={completing.stageId}
          onClose={() => {
            setCompleting(null);
            update.reset();
          }}
          pending={update.isPending}
          error={update.error}
          onComplete={(summary, stageId) =>
            update.mutate(
              {
                id: completing.ticket.id,
                changes: {
                  stage_id: stageId,
                  completion_summary: summary,
                },
              },
              { onSuccess: () => setCompleting(null) },
            )
          }
        />
      )}
      {reviewing && (
        <ReviewTicketDialog
          title={reviewing.title}
          initialSummary={reviewing.review_summary}
          pending={update.isPending}
          error={update.error}
          onClose={() => {
            setReviewing(null);
            update.reset();
          }}
          onComplete={(summary) =>
            update.mutate(
              {
                id: reviewing.id,
                changes: {
                  stage_id: "review",
                  review_summary: summary,
                  tags: reviewing.tags.some((tag) => tag.key === "needs-human")
                    ? reviewing.tags
                    : [...reviewing.tags, { key: "needs-human" }],
                },
              },
              { onSuccess: () => setReviewing(null) },
            )
          }
        />
      )}
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
}) {
  const query = useTickets(projectId, stage.id);
  const tickets = query.data?.pages.flatMap((page) => page.data) ?? [];
  const { setNodeRef, isOver } = useDroppable({ id: stage.id, disabled });
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
        <span className="tabular-nums">
          {tickets.length}
          {query.hasNextPage ? "+" : ""}
        </span>
      </h2>
      <div
        className="-mx-2 flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-2 pt-0.5 pb-2"
        style={{ paddingBottom: overlayClearance }}
      >
        {projectId && query.isPending ? (
          <Spinner aria-label="Loading tickets" className="mx-auto my-4" />
        ) : null}
        <TicketError error={query.error} />
        {query.isError && (
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
        {!query.isError && (!projectId || query.isSuccess) && tickets.length === 0 && (
          <p className="px-2 py-4 text-sm text-muted-foreground">No tickets</p>
        )}
        {query.hasNextPage && (
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
            <Link to="?ticket=new">
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
  const { setNodeRef, listeners, attributes, isDragging } = useDraggable({
    id: ticket.id,
    data: { ticket },
    disabled,
  });
  return (
    <Link
      ref={setNodeRef}
      {...attributes}
      {...listeners}
      role="link"
      to={`?ticket=${encodeURIComponent(ticket.id)}`}
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
        "flex flex-col gap-3 rounded-xl border border-border/60 bg-card p-3.5 text-left shadow-xs outline-none transition-colors hover:border-muted-foreground/50 focus-visible:ring-2 focus-visible:ring-ring",
        selected && "border-primary ring-1 ring-primary/20",
        related && "border-primary/70 bg-primary/5 ring-2 ring-primary/20",
        isDragging && "opacity-40",
      )}
    >
      <TicketCardContent ticket={ticket} />
    </Link>
  );
}

function TicketCardContent({ ticket }: { ticket: ProjectTicket }) {
  const now = useNow();
  return (
    <>
      <h3 className="text-ui leading-snug font-medium wrap-anywhere">{ticket.title}</h3>
      {ticket.tags.length > 0 && <TicketTags tags={ticket.tags} />}
      <p className="line-clamp-2 text-sm leading-relaxed text-muted-foreground wrap-anywhere">
        {ticket.latest_update}
      </p>
      <div className="flex items-center justify-between gap-2 text-sm text-muted-foreground">
        <time
          dateTime={new Date(ticket.updated_at * 1000).toISOString()}
          title={`Updated ${absoluteTime(ticket.updated_at * 1000)}`}
        >
          {relativeTime(ticket.updated_at * 1000, now.getTime())}
        </time>
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
    </>
  );
}
