import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  CheckIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  GitPullRequestIcon,
  LinkIcon,
  PencilIcon,
  PlusIcon,
  TagsIcon,
  UserRoundIcon,
  XIcon,
} from "lucide-react";
import { ProseMarkdown } from "@/components/blocks/ChatMarkdown";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useNow } from "@/hooks/useNow";
import { fetchConversationById } from "@/hooks/useOpenSessionRow";
import { useSessionBadgeStates } from "@/hooks/useSessionBadgeStates";
import { useViewerId } from "@/hooks/useViewerId";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import { findCachedListRow } from "@/lib/sessionListCache";
import { cn } from "@/lib/utils";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { getProject, initializeProjectOrchestrator } from "@/lib/projectsApi";
import {
  createTicket,
  ticketOutputs,
  TICKET_STATUSES,
  TICKET_STATUS_LABELS,
  type ProjectTicket,
  type TicketEvent,
  type TicketOutput,
  type TicketStatus,
  type TicketTag,
} from "@/lib/projectTicketsApi";
import { ProjectSessionPanel } from "./ProjectSessionPanel";
import { projectSessionPanelSearch } from "./projectHomeNavigation";
import { CompleteTicketDialog, TicketError, TicketStatusIcon, TicketTags } from "./TicketControls";
import {
  useTicket,
  useTicketActions,
  useTicketEvents,
  useTicketProject,
  useTicketSessions,
} from "./useProjectTickets";

function Panel({
  title,
  onClose,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-border/60 pr-1.5 pl-4">
        <h2 title={title} className="min-w-0 flex-1 truncate text-ui font-medium">
          {title}
        </h2>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-muted-foreground"
              aria-label="Close panel"
              onClick={onClose}
            >
              <XIcon aria-hidden />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Close</TooltipContent>
        </Tooltip>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        <div className="mx-auto w-full max-w-[72ch] space-y-6 p-5">{children}</div>
      </div>
      {footer && (
        <div className="max-h-[45%] shrink-0 overflow-y-auto overscroll-contain border-t border-border/60 p-3">
          <div className="mx-auto max-w-[72ch]">{footer}</div>
        </div>
      )}
    </section>
  );
}

export function ProjectOrchestratorPanel({
  projectName,
  onClose,
}: {
  projectName: string;
  onClose: () => void;
}) {
  const { projectId, projects, ensure } = useTicketProject(projectName);
  const client = useQueryClient();
  const project = useQuery({
    queryKey: ["project-record", projectId],
    queryFn: () => getProject(projectId!),
    enabled: projectId !== null,
  });
  const initialize = useMutation({
    mutationFn: async () => initializeProjectOrchestrator(await ensure.mutateAsync()),
    onSuccess: (value) => {
      client.setQueryData(["project-record", value.id], value);
      void client.invalidateQueries({ queryKey: ["conversations"] });
      void client.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
  if (project.data?.orchestrator_session_id)
    return (
      <ProjectSessionPanel sessionId={project.data.orchestrator_session_id} onClose={onClose} />
    );
  return (
    <Panel title="Orchestrator" onClose={onClose}>
      {projects.isLoading || (projectId && project.isPending) ? (
        <Spinner aria-label="Loading orchestrator" />
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            Create this project’s orchestrator session. It uses your project’s session defaults and
            receives ticket updates automatically.
          </p>
          <Button
            onClick={() => initialize.mutate()}
            loading={initialize.isPending}
            disabled={projects.isError || project.isError}
          >
            Create orchestrator
          </Button>
        </>
      )}
      <TicketError error={initialize.error ?? projects.error ?? project.error} />
    </Panel>
  );
}

export function ProjectTicketPanel({
  projectName,
  ticketId,
  onClose,
  onCreated,
}: {
  projectName: string;
  ticketId: string;
  onClose: () => void;
  onCreated: (ticketId: string) => void;
}) {
  const { projectId, projects, ensure } = useTicketProject(projectName);
  const client = useQueryClient();
  const create = useMutation({
    mutationFn: async (body: { title: string; description: string }) =>
      createTicket(await ensure.mutateAsync(), body),
    onSuccess: (ticket) => {
      void client.invalidateQueries({ queryKey: ["project-tickets", ticket.project_id] });
      onCreated(ticket.id);
    },
  });
  if (ticketId === "new")
    return (
      <Panel title="New ticket" onClose={onClose}>
        <TicketEditor
          onSave={(body) => create.mutate(body)}
          pending={create.isPending}
          error={create.error}
          disabled={projects.isLoading || projects.isError}
        />
        <TicketError error={projects.error} />
        <p className="text-sm text-muted-foreground">
          Starts in Open. A title is enough; the orchestrator can help clarify the rest.
        </p>
      </Panel>
    );
  if (!projectId)
    return (
      <Panel title="Ticket" onClose={onClose}>
        {projects.isLoading ? (
          <Spinner aria-label="Loading ticket" />
        ) : (
          <p role="alert">Ticket not found.</p>
        )}
        <TicketError error={projects.error} />
      </Panel>
    );
  return (
    <TicketDetails
      key={`${projectId}:${ticketId}`}
      projectId={projectId}
      ticketId={ticketId}
      onClose={onClose}
    />
  );
}

function TicketEditor({
  ticket,
  onSave,
  onCancel,
  pending,
  error,
  disabled = false,
}: {
  ticket?: ProjectTicket;
  onSave: (body: { title: string; description: string }) => void;
  onCancel?: () => void;
  pending: boolean;
  error: Error | null;
  disabled?: boolean;
}) {
  const [title, setTitle] = useState(ticket?.title ?? "");
  const [description, setDescription] = useState(ticket?.description ?? "");
  const mobile = useIsMobileViewport();
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!title.trim() || pending || disabled) return;
        onSave({ title: title.trim(), description });
      }}
    >
      <label className="grid gap-2 text-sm">
        Title
        <Input
          autoFocus={!mobile}
          name="title"
          autoComplete="off"
          required
          maxLength={160}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="What needs to get done?"
          disabled={pending}
        />
      </label>
      <label className="grid gap-2 text-sm">
        <span>
          Description <span className="text-muted-foreground">(optional)</span>
        </span>
        <Textarea
          name="description"
          rows={6}
          maxLength={3000}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="What should change? Done when…"
          disabled={pending}
        />
      </label>
      <TicketError error={error} />
      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button type="button" variant="ghost" disabled={pending} onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button type="submit" loading={pending} disabled={disabled || !title.trim()}>
          {ticket ? "Save changes" : "Create ticket"}
        </Button>
      </div>
    </form>
  );
}

function TicketDetails({
  projectId,
  ticketId,
  onClose,
}: {
  projectId: string;
  ticketId: string;
  onClose: () => void;
}) {
  const query = useTicket(projectId, ticketId);
  const { update, comment } = useTicketActions(projectId);
  const [editing, setEditing] = useState(false);
  const [completing, setCompleting] = useState(false);
  const ticket = query.data;
  const humanInput = ticket?.tags.find((tag) => tag.key === "needs-human")?.value;
  if (!ticket)
    return (
      <Panel title="Ticket" onClose={onClose}>
        {query.isPending && <Spinner aria-label="Loading ticket" />}
        <TicketError error={query.error} />
        {query.isError && (
          <Button variant="ghost" onClick={() => void query.refetch()}>
            Try again
          </Button>
        )}
      </Panel>
    );
  return (
    <Panel
      title={ticket.title}
      onClose={onClose}
      footer={<TicketCommentComposer ticketId={ticketId} comment={comment} />}
    >
      {editing ? (
        <TicketEditor
          ticket={ticket}
          pending={update.isPending}
          error={update.error}
          onCancel={() => setEditing(false)}
          onSave={(changes) =>
            update.mutate({ id: ticketId, changes }, { onSuccess: () => setEditing(false) })
          }
        />
      ) : (
        <div className="space-y-4">
          <div className="flex items-start gap-2">
            <h3 className="min-w-0 flex-1 text-xl leading-snug font-semibold text-pretty wrap-anywhere">
              {ticket.title}
            </h3>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  className="text-muted-foreground"
                  aria-label="Edit ticket"
                  onClick={() => {
                    update.reset();
                    setEditing(true);
                  }}
                >
                  <PencilIcon aria-hidden />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Edit ticket</TooltipContent>
            </Tooltip>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={ticket.status}
              disabled={update.isPending}
              onValueChange={(value) => {
                update.reset();
                if (value === "done") setCompleting(true);
                else update.mutate({ id: ticketId, changes: { status: value as TicketStatus } });
              }}
            >
              <SelectTrigger
                size="sm"
                aria-label="Ticket status"
                className="shrink-0 border-border/60 bg-muted/30"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TICKET_STATUSES.map((status) => (
                  <SelectItem key={status} value={status}>
                    <TicketStatusIcon status={status} />
                    {TICKET_STATUS_LABELS[status]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <TicketTags tags={ticket.tags} />
            <TagEditor
              tags={ticket.tags}
              disabled={update.isPending}
              error={update.error}
              onChange={(tags) => update.mutate({ id: ticketId, changes: { tags } })}
            />
          </div>
          {humanInput && (
            <p className="text-sm leading-relaxed text-warning wrap-anywhere">{humanInput}</p>
          )}
          {ticket.description ? (
            <ProseMarkdown className="text-ui leading-relaxed">{ticket.description}</ProseMarkdown>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2 text-muted-foreground"
              onClick={() => {
                update.reset();
                setEditing(true);
              }}
            >
              <PlusIcon aria-hidden />
              Add description
            </Button>
          )}
        </div>
      )}
      {!editing && !completing && <TicketError error={update.error} />}
      {ticket.completion_summary && (
        <section className="space-y-2 rounded-lg bg-success/5 p-3.5">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <CheckIcon aria-hidden className="size-4 text-success" />
            Completion summary
          </h3>
          <ProseMarkdown className="text-ui">{ticket.completion_summary}</ProseMarkdown>
        </section>
      )}
      <TicketOutputs projectId={projectId} ticketId={ticketId} />
      <LinkedSessions projectId={projectId} ticketId={ticketId} />
      <TicketActivity projectId={projectId} ticketId={ticketId} />
      {completing && (
        <CompleteTicketDialog
          title={ticket.title}
          pending={update.isPending}
          error={update.error}
          onClose={() => {
            setCompleting(false);
            update.reset();
          }}
          onComplete={(summary) =>
            update.mutate(
              { id: ticketId, changes: { status: "done", completion_summary: summary } },
              { onSuccess: () => setCompleting(false) },
            )
          }
        />
      )}
    </Panel>
  );
}

function TicketCommentComposer({
  ticketId,
  comment,
}: {
  ticketId: string;
  comment: ReturnType<typeof useTicketActions>["comment"];
}) {
  const [text, setText] = useState("");
  const [attaching, setAttaching] = useState(false);
  const [outputUrl, setOutputUrl] = useState("");
  const [outputType, setOutputType] = useState<TicketOutput["type"]>("pull_request");
  return (
    <form
      className="space-y-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (!text.trim() || comment.isPending) return;
        comment.mutate(
          {
            id: ticketId,
            body: {
              text: text.trim(),
              outputs:
                attaching && outputUrl.trim() ? [{ type: outputType, url: outputUrl.trim() }] : [],
            },
          },
          {
            onSuccess: () => {
              setText("");
              setOutputUrl("");
              setAttaching(false);
            },
          },
        );
      }}
    >
      <div className="rounded-xl border border-input bg-background focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/30">
        <Textarea
          name="comment"
          aria-label="Add a comment"
          required
          maxLength={2000}
          rows={2}
          className="max-h-36 min-h-16 resize-none rounded-none border-0 px-3 shadow-none focus-visible:ring-0"
          value={text}
          disabled={comment.isPending}
          onChange={(event) => setText(event.target.value)}
          placeholder="Add a comment…"
        />
        {attaching && (
          <div className="flex flex-wrap items-center gap-2 border-t border-border/60 p-2">
            <Select
              value={outputType}
              disabled={comment.isPending}
              onValueChange={(value) => setOutputType(value as TicketOutput["type"])}
            >
              <SelectTrigger size="sm" aria-label="Output type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="pull_request">Pull request</SelectItem>
                <SelectItem value="link">Link</SelectItem>
              </SelectContent>
            </Select>
            <Input
              name="output_url"
              type="url"
              pattern="https?://.+"
              autoComplete="off"
              spellCheck={false}
              maxLength={2048}
              className="min-w-36 flex-1"
              value={outputUrl}
              disabled={comment.isPending}
              onChange={(event) => setOutputUrl(event.target.value)}
              placeholder="https://…"
              aria-label="Output URL"
            />
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              disabled={comment.isPending}
              aria-label="Remove attachment"
              onClick={() => {
                setAttaching(false);
                setOutputUrl("");
              }}
            >
              <XIcon aria-hidden />
            </Button>
          </div>
        )}
        <div className="flex items-center justify-between gap-2 p-2 pt-0">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-muted-foreground"
            aria-expanded={attaching}
            disabled={comment.isPending || attaching}
            onClick={() => setAttaching(true)}
          >
            <LinkIcon aria-hidden />
            Attach link
          </Button>
          <Button type="submit" size="sm" loading={comment.isPending} disabled={!text.trim()}>
            Comment
          </Button>
        </div>
      </div>
      <TicketError error={comment.error} />
      <span role="status" className="sr-only">
        {comment.isSuccess ? "Comment added" : ""}
      </span>
    </form>
  );
}

function TagEditor({
  tags,
  disabled,
  error,
  onChange,
}: {
  tags: TicketTag[];
  disabled: boolean;
  error: Error | null;
  onChange: (tags: TicketTag[]) => void;
}) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size={tags.length ? "icon-sm" : "sm"}
          className="text-muted-foreground"
          aria-label={tags.length ? "Edit tags" : "Add tags"}
        >
          <TagsIcon aria-hidden />
          {tags.length === 0 && "Tags"}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        aria-label="Edit tags"
        className="max-w-[calc(100vw-2rem)] gap-3 p-3"
      >
        <h3 className="text-sm font-medium">Tags</h3>
        {tags.length > 0 && (
          <ul className="max-h-48 space-y-1 overflow-y-auto">
            {tags.map((tag) => (
              <li key={tag.key} className="flex min-w-0 items-center gap-2">
                <span className="min-w-0 flex-1 text-sm wrap-anywhere">
                  {tag.key}
                  {tag.value && <span className="text-muted-foreground">: {tag.value}</span>}
                </span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  disabled={disabled}
                  aria-label={`Remove tag ${tag.key}`}
                  onClick={() => onChange(tags.filter((item) => item.key !== tag.key))}
                >
                  <XIcon aria-hidden />
                </Button>
              </li>
            ))}
          </ul>
        )}
        <form
          className="grid gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (disabled || !key.trim()) return;
            const next = { key: key.trim(), ...(value ? { value } : {}) };
            onChange([...tags.filter((tag) => tag.key !== next.key), next]);
          }}
        >
          <Input
            name="tag_key"
            autoComplete="off"
            aria-label="Tag key"
            placeholder="Key, e.g. needs-human…"
            maxLength={64}
            required
            disabled={disabled}
            value={key}
            onChange={(event) => setKey(event.target.value)}
          />
          <Input
            name="tag_value"
            autoComplete="off"
            aria-label="Tag value"
            placeholder="Value (optional)…"
            maxLength={256}
            disabled={disabled}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <Button
            type="submit"
            size="sm"
            className="justify-self-end"
            disabled={
              disabled ||
              !key.trim() ||
              (tags.length >= 20 && !tags.some((tag) => tag.key === key.trim()))
            }
          >
            <PlusIcon aria-hidden />
            Save tag
          </Button>
        </form>
        <TicketError error={error} />
      </PopoverContent>
    </Popover>
  );
}

function TicketOutputs({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useTicketEvents(projectId, ticketId, "output");
  const outputs = ticketOutputs(query.data?.pages.flatMap((page) => page.data) ?? []);
  if (outputs.length === 0 && !query.error && !query.hasNextPage) return null;
  return (
    <section aria-label="Outputs" className="space-y-2">
      <h3 className="text-sm font-medium">Outputs</h3>
      <TicketError error={query.error} />
      {outputs.length > 0 && (
        <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60">
          {outputs.map((output) => (
            <li key={`${output.type}:${output.url}`}>
              <TicketOutputLink output={output} />
            </li>
          ))}
        </ul>
      )}
      {query.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          loading={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          More outputs
        </Button>
      )}
    </section>
  );
}

function TicketOutputLink({ output, inline = false }: { output: TicketOutput; inline?: boolean }) {
  const url = parseHttpUrl(output.url);
  if (!url) return null;
  const pullRequest =
    output.type === "pull_request" ? url.pathname.match(/^\/(.+)\/pull\/(\d+)(?:\/|$)/) : null;
  const label = pullRequest
    ? `${pullRequest[1]} #${pullRequest[2]}`
    : `${url.host}${url.pathname === "/" ? "" : url.pathname}${url.search}${url.hash}`;
  return (
    <a
      href={output.url}
      target="_blank"
      rel="noopener noreferrer"
      title={output.url}
      className={cn(
        "min-w-0 rounded-sm text-foreground underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        inline
          ? "wrap-anywhere hover:underline"
          : "flex items-center gap-3 p-3 transition-colors hover:bg-muted/40 focus-visible:ring-inset",
      )}
    >
      {!inline &&
        (output.type === "pull_request" ? (
          <GitPullRequestIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        ) : (
          <LinkIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        ))}
      <span className={inline ? undefined : "min-w-0 flex-1"}>
        <span className={inline ? undefined : "block truncate text-sm font-medium"}>{label}</span>
        {!inline && (
          <span className="block text-sm text-muted-foreground">
            {output.type === "pull_request" ? "Pull request" : "Link"}
            {pullRequest ? ` on ${url.host}` : ""}
          </span>
        )}
      </span>
      {!inline && (
        <ExternalLinkIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
      )}
    </a>
  );
}

function SessionLink({
  sessionId,
  showStatus = false,
}: {
  sessionId: string;
  showStatus?: boolean;
}) {
  const mobile = useIsMobileViewport();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["conversation-backfill", sessionId],
    queryFn: () => fetchConversationById(sessionId, client),
    initialData: () => findCachedListRow(client, sessionId),
    staleTime: 30_000,
    refetchInterval: showStatus ? 15_000 : false,
    retry: false,
  });
  const session = query.data;
  const [state] = useSessionBadgeStates(session ? [session] : []);
  const title = session ? conversationDisplayLabel(session) : `Session ${sessionId.slice(0, 8)}`;
  const unavailable = query.isError || session === null;
  const status = unavailable
    ? "Unavailable"
    : !session
      ? "Loading…"
      : session.archived
        ? "Archived"
        : state?.kind === "running" || state?.kind === "starting"
          ? "Working"
          : state?.kind === "error"
            ? "Error"
            : state?.kind === "disconnected"
              ? "Disconnected"
              : "Idle";
  return (
    <Link
      className={cn(
        "rounded-sm text-foreground underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        showStatus
          ? "flex min-w-0 items-center gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-muted/40 focus-visible:ring-inset"
          : "inline-block max-w-full align-bottom wrap-anywhere hover:underline",
      )}
      title={`${title} (${sessionId})`}
      to={mobile ? `/c/${encodeURIComponent(sessionId)}` : projectSessionPanelSearch(sessionId)}
    >
      {showStatus && <BotIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />}
      <span className={showStatus ? "min-w-0 flex-1 truncate text-sm font-medium" : undefined}>
        {title}
      </span>
      {showStatus && (
        <>
          <span className="flex shrink-0 items-center gap-1.5 text-sm text-muted-foreground">
            {!unavailable && !session?.archived && state && <SessionStateBadge state={state} />}
            {state?.kind !== "awaiting" || unavailable || session?.archived ? (
              <span>{status}</span>
            ) : null}
          </span>
          <ChevronRightIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
        </>
      )}
    </Link>
  );
}

function LinkedSessions({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useTicketSessions(projectId, ticketId);
  const sessions = query.data?.pages.flatMap((page) => page.data) ?? [];
  if (sessions.length === 0 && !query.error) return null;
  return (
    <section aria-label="Linked sessions" className="space-y-2 text-sm">
      <h3 className="font-medium">Sessions</h3>
      <TicketError error={query.error} />
      {sessions.length > 0 && (
        <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60">
          {sessions.map((session) => (
            <li key={session.session_id}>
              <SessionLink sessionId={session.session_id} showStatus />
            </li>
          ))}
        </ul>
      )}
      {query.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          loading={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          More sessions
        </Button>
      )}
    </section>
  );
}

function ActivityChange({ event }: { event: TicketEvent }) {
  if (event.kind === "created") return <>created this ticket</>;
  if (event.kind === "output") {
    const output = ticketOutputs([event])[0];
    return <>added {output ? <TicketOutputLink output={output} inline /> : "an output"}</>;
  }
  if (typeof event.payload.session_id === "string")
    return (
      <>
        {event.payload.action === "attach" ? "linked" : "unlinked"}{" "}
        <SessionLink sessionId={event.payload.session_id} />
      </>
    );
  const changes = event.payload.changes as Partial<ProjectTicket> | undefined;
  if (changes?.status && TICKET_STATUSES.includes(changes.status))
    return (
      <>
        moved to{" "}
        <span className="font-medium text-foreground">{TICKET_STATUS_LABELS[changes.status]}</span>
      </>
    );
  const fields = Object.keys(changes ?? {}).map((field) => field.replaceAll("_", " "));
  return <>updated {fields.join(", ") || "the ticket"}</>;
}

function TicketActivity({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useTicketEvents(projectId, ticketId);
  const now = useNow();
  const viewerId = useViewerId();
  const events = query.data?.pages.flatMap((page) => page.data) ?? [];
  if (!query.isPending && !query.error && !query.hasNextPage && events.length === 0) return null;
  return (
    <section aria-label="Ticket activity" className="space-y-4 border-t border-border/60 pt-5">
      <h3 className="text-sm font-medium">Activity</h3>
      <TicketError error={query.error} />
      {query.isPending && <Spinner aria-label="Loading activity" />}
      <ol>
        {events.map((event) => (
          <TicketActivityItem
            key={event.id}
            event={event}
            nowMs={now.getTime()}
            viewerId={viewerId}
          />
        ))}
      </ol>
      {query.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          loading={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          More activity
        </Button>
      )}
    </section>
  );
}

function TicketActivityItem({
  event,
  nowMs,
  viewerId,
}: {
  event: TicketEvent;
  nowMs: number;
  viewerId: string | null;
}) {
  const note = event.kind === "comment" || event.kind === "execution";
  const changes = event.payload.changes as Partial<ProjectTicket> | undefined;
  const body =
    typeof event.payload.text === "string" ? event.payload.text : changes?.completion_summary;
  const agent = !!(event.session_id || event.agent_id);
  const author =
    event.user_id && event.user_id === viewerId
      ? "You"
      : (event.user_id ?? (agent ? "Agent" : "System"));
  const actor = event.session_id ? (
    <SessionLink sessionId={event.session_id} />
  ) : (
    <span className="text-foreground">{author}</span>
  );
  const status =
    changes?.status && TICKET_STATUSES.includes(changes.status) ? changes.status : null;
  const age = relativeTime(event.created_at * 1000, nowMs);
  return (
    <li className="relative flex min-w-0 gap-3 pb-4 last:pb-0 before:absolute before:top-6 before:bottom-0 before:left-3 before:w-px before:bg-border/60 last:before:hidden">
      <span
        className={cn(
          "relative flex size-6 shrink-0 items-center justify-center bg-background text-muted-foreground",
          note && "rounded-full bg-muted",
        )}
      >
        {note ? (
          agent ? (
            <BotIcon aria-hidden className="size-3.5" />
          ) : (
            <UserRoundIcon aria-hidden className="size-3.5" />
          )
        ) : status ? (
          <TicketStatusIcon status={status} />
        ) : event.kind === "output" ? (
          event.payload.type === "pull_request" ? (
            <GitPullRequestIcon aria-hidden className="size-3.5" />
          ) : (
            <LinkIcon aria-hidden className="size-3.5" />
          )
        ) : (
          <span aria-hidden className="size-1.5 rounded-full bg-muted-foreground/50" />
        )}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <div className="flex items-start gap-3 text-sm leading-6 text-muted-foreground">
          <div className="min-w-0 flex-1 wrap-anywhere">
            <span className={note ? "font-medium" : undefined}>{actor}</span>
            {note ? (
              event.kind === "execution" && <span className="ml-2">Work update</span>
            ) : (
              <>
                {" "}
                <ActivityChange event={event} />
              </>
            )}
          </div>
          <time
            className="shrink-0 text-sm leading-6 tabular-nums"
            dateTime={new Date(event.created_at * 1000).toISOString()}
            title={absoluteTime(event.created_at * 1000)}
          >
            {age === "now" ? "Just now" : `${age} ago`}
          </time>
        </div>
        {body && <ProseMarkdown className="text-ui leading-relaxed">{body}</ProseMarkdown>}
      </div>
    </li>
  );
}
