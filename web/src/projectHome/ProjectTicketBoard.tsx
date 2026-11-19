import { useRef, useState } from "react";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useNow } from "@/hooks/useNow";
import { Link } from "@/lib/routing";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import {
  TICKET_STATUSES,
  TICKET_STATUS_LABELS,
  type ProjectTicket,
  type TicketStatus,
} from "@/lib/projectTicketsApi";
import { cn } from "@/lib/utils";
import { CompleteTicketDialog, TicketError, TicketStatusIcon, TicketTags } from "./TicketControls";
import { useTicketActions, useTicketProject, useTickets } from "./useProjectTickets";

const STATUS_CLASS: Record<TicketStatus, string> = {
  open: "bg-muted text-muted-foreground",
  in_progress: "bg-session-active/15 text-session-active",
  done: "bg-success/15 text-success",
};

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
  const [completing, setCompleting] = useState<ProjectTicket | null>(null);
  const dragEnded = useRef(0);
  const { update } = useTicketActions(projectId ?? "");
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <TicketError error={completing ? null : update.error} />
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
          const status = over?.id as TicketStatus | undefined;
          if (!ticket || !status || !TICKET_STATUSES.includes(status) || ticket.status === status)
            return;
          update.reset();
          if (status === "done") setCompleting(ticket);
          else update.mutate({ id: ticket.id, changes: { status } });
        }}
      >
        <div
          role="region"
          aria-label={`${projectName} ticket board`}
          data-testid="project-ticket-board"
          className="flex min-h-0 flex-1 gap-4 overflow-x-auto px-4 pt-1 pb-4"
        >
          {TICKET_STATUSES.map((status) => (
            <TicketColumn
              key={status}
              status={status}
              projectId={projectId}
              selectedTicketId={selectedTicketId}
              dragging={dragged !== null}
              disabled={update.isPending}
              overlayClearance={overlayClearance}
              suppressClick={() => Date.now() - dragEnded.current < 250}
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
          title={completing.title}
          onClose={() => {
            setCompleting(null);
            update.reset();
          }}
          pending={update.isPending}
          error={update.error}
          onComplete={(summary) =>
            update.mutate(
              { id: completing.id, changes: { status: "done", completion_summary: summary } },
              { onSuccess: () => setCompleting(null) },
            )
          }
        />
      )}
    </div>
  );
}

function TicketColumn({
  projectId,
  status,
  selectedTicketId,
  dragging,
  disabled,
  suppressClick,
  overlayClearance,
}: {
  projectId: string | null;
  status: TicketStatus;
  selectedTicketId: string | null;
  dragging: boolean;
  disabled: boolean;
  suppressClick: () => boolean;
  overlayClearance?: string;
}) {
  const query = useTickets(projectId, status);
  const tickets = query.data?.pages.flatMap((page) => page.data) ?? [];
  const { setNodeRef, isOver } = useDroppable({ id: status, disabled });
  return (
    <section
      ref={setNodeRef}
      aria-label={TICKET_STATUS_LABELS[status]}
      className={cn(
        "flex min-w-60 flex-1 basis-0 flex-col rounded-xl border border-transparent bg-muted/30 px-2 pt-2 transition-colors",
        dragging && "border-dashed border-muted-foreground/50",
        dragging && isOver && "border-primary bg-primary/10",
      )}
    >
      <h2 className="flex shrink-0 items-center justify-between px-1.5 pt-1.5 pb-3 text-sm font-medium text-muted-foreground">
        <Badge className={cn("border-transparent", STATUS_CLASS[status])}>
          <TicketStatusIcon status={status} />
          {TICKET_STATUS_LABELS[status]}
        </Badge>
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
        {status === "open" && (
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
}: {
  ticket: ProjectTicket;
  selected: boolean;
  disabled: boolean;
  suppressClick: () => boolean;
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
      onClick={(event) => {
        if (suppressClick()) event.preventDefault();
      }}
      className={cn(
        "flex flex-col gap-3 rounded-xl border border-border/60 bg-card p-3.5 text-left shadow-xs outline-none transition-colors hover:border-muted-foreground/50 focus-visible:ring-2 focus-visible:ring-ring",
        selected && "border-primary ring-1 ring-primary/20",
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
