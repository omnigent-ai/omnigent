// A project's sessions as cards in the sidebar's Status columns. Columns follow
// live status; dragging a card between Unread and Done marks it read or unread.

import {
  createContext,
  Fragment,
  useContext,
  useEffect,
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
  type LucideIcon,
} from "lucide-react";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useProjectSessions, useProjects, type Conversation } from "@/hooks/useConversations";
import { useHosts, type Host } from "@/hooks/useHosts";
import { getSessionState } from "@/hooks/useSessionState";
import { useOptionalSidebarShow } from "@/hooks/useSidebarData";
import {
  isConversationUnseen,
  markConversationRead,
  markConversationUnread,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { useViewerId } from "@/hooks/useViewerId";
import { fetchLastAssistantMessage, leadingSentences } from "@/lib/lastAssistantText";
import { Link } from "@/lib/routing";
import { hasSessionDraft, useSessionDraftIds } from "@/lib/sessionDrafts";
import { cn } from "@/lib/utils";
import {
  conversationDisplayLabel,
  sessionBelongsToProject,
  statusBucket,
  type StatusBucket,
} from "@/shell/sidebarNav";
import {
  SessionEnvironmentLabel,
  SessionHarnessIcon,
  sessionRepoName,
  SessionUpdatedLabel,
  useSessionBranch,
} from "@/shell/sessionRowMeta";
import {
  KANBAN_COLUMNS,
  kanbanColumns,
  kanbanDropAction,
  takeArrivals,
  type KanbanCard,
} from "./projectKanban";

// A drag that ends over its own card still fires a click; ignore it this long.
const DRAG_CLICK_SUPPRESS_MS = 250;
const RESPONSE_PREVIEW_SENTENCES = 2;

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

function columnOf(row: Conversation): StatusBucket {
  return statusBucket(row, isUnseen(row), hasSessionDraft(row.id));
}

export function ProjectKanbanBoard({
  projectName,
  overlayClearance,
}: {
  projectName: string;
  /** Room below each lane's last card, so it can scroll clear of an overlay such as the composer. */
  overlayClearance?: string;
}) {
  const viewerId = useViewerId();
  const { data: projects } = useProjects();
  const projectId = projects?.find((project) => project.name === projectName)?.id ?? null;
  const query = useProjectSessions(projectName, true);
  // Read marks and drafts live outside the query cache; re-render when they change.
  useUnseenTick();
  useSessionDraftIds();
  const rows = useMemo(
    () =>
      (query.data?.pages.flatMap((page) => page.data) ?? []).filter(
        (row) =>
          !row.archived &&
          sessionBelongsToProject(row, { id: projectId, name: projectName }, viewerId),
      ),
    [query.data, projectId, projectName, viewerId],
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
      <div className="flex justify-center py-10">
        <Spinner className="size-5 text-muted-foreground" aria-label="Loading sessions" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div role="alert" className="flex flex-col items-center gap-3 p-6">
        <p className="text-muted-foreground">Couldn’t load this project’s sessions.</p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

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
            if (action === "mark_read") {
              for (const member of card.members) {
                if (isUnseen(member)) markConversationRead(member.id, member.updated_at);
              }
            } else if (action === "mark_unread") {
              markConversationUnread(card.conversation.id, card.conversation.updated_at);
            }
          }}
        >
          <div className="flex min-h-0 flex-1 flex-col">
            <div
              role="region"
              aria-label={`${projectName} kanban board`}
              data-testid="project-kanban-board"
              className="flex min-h-0 flex-1 justify-center-safe gap-3 overflow-x-auto overflow-y-hidden px-4 pt-1 pb-4"
            >
              {KANBAN_COLUMNS.map((column) => (
                <KanbanColumn
                  key={column}
                  column={column}
                  cards={columns.get(column)!}
                  dropAllowed={dragged !== null && kanbanDropAction(dragged, column) !== null}
                  suppressClick={() => Date.now() - dragEndedAt.current < DRAG_CLICK_SUPPRESS_MS}
                  overlayClearance={overlayClearance}
                />
              ))}
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
            {dragged && <KanbanCardBody card={dragged} className="w-64 rotate-1 shadow-md" />}
          </DragOverlay>
        </DndContext>
      </ArrivedIdsContext.Provider>
    </HostsByIdContext.Provider>
  );
}

function KanbanColumn({
  column,
  cards,
  dropAllowed,
  suppressClick,
  overlayClearance,
}: {
  column: StatusBucket;
  cards: KanbanCard[];
  /** Whether the card being dragged can drop here. */
  dropAllowed: boolean;
  suppressClick: () => boolean;
  overlayClearance?: string;
}) {
  const { setNodeRef, isOver } = useDroppable({ id: `column:${column}`, data: { column } });
  return (
    <section
      ref={setNodeRef}
      aria-label={column}
      data-testid={`kanban-column-${column}`}
      className={cn(
        "flex max-w-[calc(440px+1rem)] min-w-52 flex-1 basis-0 flex-col rounded-xl bg-muted/50 px-2 pt-2 transition-colors",
        dropAllowed && "outline-1 outline-muted-foreground/50 outline-dashed",
        dropAllowed && isOver && "bg-primary/10 outline-primary",
      )}
    >
      <h2 className="flex shrink-0 items-center justify-between px-1.5 pt-1.5 pb-3 text-sm font-medium text-muted-foreground">
        <Badge className={cn("border-transparent", COLUMN_TAG_CLASS[column])}>{column}</Badge>
        <span className="tabular-nums">{cards.length}</span>
      </h2>
      {/* The lane's own scroller; its side padding keeps card rings from clipping. */}
      <ul
        className="-mx-2 flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-2 pt-0.5 pb-2"
        style={{ paddingBottom: overlayClearance }}
      >
        {cards.map((card) => (
          <li key={card.conversation.id}>
            <DraggableKanbanCard card={card} suppressClick={suppressClick} />
          </li>
        ))}
        {cards.length === 0 && (
          <li className="px-1.5 py-1 text-sm text-muted-foreground">No sessions</li>
        )}
      </ul>
    </section>
  );
}

function DraggableKanbanCard({
  card,
  suppressClick,
}: {
  card: KanbanCard;
  suppressClick: () => boolean;
}) {
  const { setNodeRef, listeners, isDragging } = useDraggable({
    id: card.conversation.id,
    data: { card },
  });
  return (
    // Listeners only: the card keeps its link semantics rather than dnd-kit's button role.
    <Link
      ref={setNodeRef}
      {...listeners}
      to={`/c/${card.conversation.id}`}
      draggable={false}
      data-testid="kanban-card"
      onClick={(event) => {
        if (suppressClick()) event.preventDefault();
      }}
      className={cn(
        "block rounded-lg focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
        isDragging && "opacity-40",
      )}
    >
      <KanbanCardBody card={card} className="hover:border-foreground/20" />
    </Link>
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

function KanbanCardBody({ card, className }: { card: KanbanCard; className?: string }) {
  const { conversation, members } = card;
  const state =
    getSessionState(conversation) ?? (isUnseen(conversation) ? { kind: "unseen" as const } : null);
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
        "flex flex-col gap-1.5 rounded-lg border bg-card p-3 text-ui text-foreground shadow-sm transition-[color,background-color,border-color,box-shadow] duration-700",
        arrived && "border-primary/60 ring-2 ring-primary/20",
        className,
      )}
    >
      <div className="flex min-w-0 items-start gap-2">
        {show.includes("harness") && (
          <span className="flex h-5 shrink-0 items-center">
            <SessionHarnessIcon
              conversation={conversation}
              testId="kanban-card-harness"
              withFallback
            />
          </span>
        )}
        <span className="line-clamp-2 min-w-0 flex-1 font-medium">
          {conversationDisplayLabel(conversation)}
        </span>
        {state && <SessionStateBadge state={state} />}
      </div>
      {responsePreview && (
        <p
          data-testid="kanban-card-response"
          className="line-clamp-2 text-sm text-muted-foreground"
        >
          {responsePreview}
        </p>
      )}
      {metaParts.length > 0 && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm text-primary-cta">
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
