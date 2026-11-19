import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLinkIcon, GitPullRequestIcon, PencilIcon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link } from "@/lib/routing";
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
import { CompleteTicketDialog, TicketError, TicketTags } from "./TicketControls";
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
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      <header className="flex h-11 shrink-0 items-center gap-2 border-b pr-1.5 pl-4">
        <h2 className="min-w-0 flex-1 truncate text-sm font-medium">{title}</h2>
        <Button variant="ghost" size="icon-sm" aria-label="Close panel" onClick={onClose}>
          <XIcon />
        </Button>
      </header>
      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4">{children}</div>
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
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSave({ title: title.trim(), description });
      }}
    >
      <label className="grid gap-2 text-sm">
        Title
        <Input
          autoFocus
          required
          maxLength={160}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="What needs to get done?"
        />
      </label>
      <label className="grid gap-2 text-sm">
        Description <span className="text-muted-foreground">Optional</span>
        <Textarea
          rows={6}
          maxLength={3000}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="What should change? Done when…"
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
  const [text, setText] = useState("");
  const [outputUrl, setOutputUrl] = useState("");
  const [outputType, setOutputType] = useState<TicketOutput["type"]>("pull_request");
  const ticket = query.data;
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
    <Panel title="Ticket" onClose={onClose}>
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
        <>
          <div className="flex items-start gap-2">
            <h3 className="min-w-0 flex-1 text-lg font-medium wrap-anywhere">{ticket.title}</h3>
            <Button
              size="icon-sm"
              variant="ghost"
              aria-label="Edit ticket"
              onClick={() => {
                update.reset();
                setEditing(true);
              }}
            >
              <PencilIcon />
            </Button>
          </div>
          <Select
            value={ticket.status}
            disabled={update.isPending}
            onValueChange={(value) => {
              update.reset();
              if (value === "done") setCompleting(true);
              else update.mutate({ id: ticketId, changes: { status: value as TicketStatus } });
            }}
          >
            <SelectTrigger aria-label="Ticket status">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TICKET_STATUSES.map((status) => (
                <SelectItem key={status} value={status}>
                  {TICKET_STATUS_LABELS[status]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <p className="text-sm whitespace-pre-wrap wrap-anywhere text-muted-foreground">
            {ticket.description || "No description yet."}
          </p>
        </>
      )}
      <TagEditor
        tags={ticket.tags}
        disabled={update.isPending}
        onChange={(tags) => update.mutate({ id: ticketId, changes: { tags } })}
      />
      {!editing && !completing && <TicketError error={update.error} />}
      {ticket.completion_summary && (
        <section className="space-y-2">
          <h3 className="text-sm font-medium">Completion summary</h3>
          <p className="text-sm whitespace-pre-wrap wrap-anywhere">{ticket.completion_summary}</p>
        </section>
      )}
      <TicketOutputs projectId={projectId} ticketId={ticketId} />
      <LinkedSessions projectId={projectId} ticketId={ticketId} />
      <TicketActivity projectId={projectId} ticketId={ticketId} />
      <form
        className="space-y-3 border-t pt-4"
        onSubmit={(event) => {
          event.preventDefault();
          comment.mutate(
            {
              id: ticketId,
              body: {
                text: text.trim(),
                outputs: outputUrl.trim() ? [{ type: outputType, url: outputUrl.trim() }] : [],
              },
            },
            {
              onSuccess: () => {
                setText("");
                setOutputUrl("");
              },
            },
          );
        }}
      >
        <label className="grid gap-2 text-sm">
          Add a comment
          <Textarea
            required
            maxLength={2000}
            rows={3}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Progress, a question, or what was delivered…"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <Select
            value={outputType}
            onValueChange={(value) => setOutputType(value as TicketOutput["type"])}
          >
            <SelectTrigger aria-label="Output type">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="pull_request">Pull request</SelectItem>
              <SelectItem value="link">Link</SelectItem>
            </SelectContent>
          </Select>
          <Input
            type="url"
            maxLength={2048}
            className="min-w-36 flex-1"
            value={outputUrl}
            onChange={(event) => setOutputUrl(event.target.value)}
            placeholder="Optional URL"
            aria-label="Output URL"
          />
        </div>
        <TicketError error={comment.error} />
        <div className="flex justify-end">
          <Button type="submit" loading={comment.isPending} disabled={!text.trim()}>
            Comment
          </Button>
        </div>
      </form>
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

function TagEditor({
  tags,
  disabled,
  onChange,
}: {
  tags: TicketTag[];
  disabled: boolean;
  onChange: (tags: TicketTag[]) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2">
        <h3 className="flex-1 text-sm font-medium">Tags</h3>
        <Button variant="ghost" size="sm" onClick={() => setEditing(!editing)}>
          {editing ? "Close" : "Edit tags"}
        </Button>
      </div>
      <TicketTags tags={tags} />
      {editing && (
        <>
          {tags.map((tag) => (
            <Button
              key={tag.key}
              variant="ghost"
              size="sm"
              disabled={disabled}
              aria-label={`Remove tag ${tag.key}`}
              onClick={() => onChange(tags.filter((item) => item.key !== tag.key))}
            >
              <XIcon />
              {tag.key}
            </Button>
          ))}
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              const next = { key: key.trim(), ...(value ? { value } : {}) };
              onChange([...tags.filter((tag) => tag.key !== next.key), next]);
            }}
          >
            <Input
              aria-label="Tag key"
              placeholder="Key, e.g. needs-human"
              maxLength={64}
              required
              value={key}
              onChange={(event) => setKey(event.target.value)}
            />
            <Input
              aria-label="Tag value"
              placeholder="Value (optional)"
              maxLength={256}
              value={value}
              onChange={(event) => setValue(event.target.value)}
            />
            <Button
              type="submit"
              size="sm"
              disabled={
                disabled ||
                !key.trim() ||
                (tags.length >= 20 && !tags.some((tag) => tag.key === key.trim()))
              }
            >
              <PlusIcon />
              Save tag
            </Button>
          </form>
        </>
      )}
    </section>
  );
}

function TicketOutputs({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useTicketEvents(projectId, ticketId, "output");
  const outputs = ticketOutputs(query.data?.pages.flatMap((page) => page.data) ?? []);
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-medium">Outputs</h3>
      <TicketError error={query.error} />
      {query.isPending && <Spinner aria-label="Loading outputs" />}
      {query.isSuccess && outputs.length === 0 && (
        <p className="text-sm text-muted-foreground">No outputs yet.</p>
      )}
      {outputs.map((output) => (
        <a
          key={`${output.type}:${output.url}`}
          href={output.url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-2 text-sm text-primary underline-offset-4 hover:underline"
        >
          <span>
            {output.type === "pull_request" ? (
              <GitPullRequestIcon className="size-4" />
            ) : (
              <ExternalLinkIcon className="size-4" />
            )}
          </span>
          <span className="truncate">{output.url}</span>
        </a>
      ))}
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

function SessionLink({ sessionId, children }: { sessionId: string; children?: ReactNode }) {
  const mobile = useIsMobileViewport();
  return (
    <Link
      className="text-primary hover:underline"
      to={mobile ? `/c/${sessionId}` : projectSessionPanelSearch(sessionId)}
    >
      {children ?? `Session ${sessionId.slice(0, 8)}`}
    </Link>
  );
}

function LinkedSessions({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useTicketSessions(projectId, ticketId);
  const sessions = query.data?.pages.flatMap((page) => page.data) ?? [];
  if (sessions.length === 0 && !query.error) return null;
  return (
    <section className="space-y-2 text-sm">
      <h3 className="font-medium">Sessions</h3>
      <TicketError error={query.error} />
      {sessions.map((session) => (
        <p key={session.session_id}>
          <SessionLink sessionId={session.session_id} />
        </p>
      ))}
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

function activityText(event: TicketEvent): string {
  if (typeof event.payload.text === "string") return event.payload.text;
  if (event.kind === "created") return "Created the ticket";
  if (event.kind === "output")
    return `Added ${event.payload.type === "pull_request" ? "pull request" : "link"}: ${event.payload.url}`;
  if (event.payload.session_id)
    return `${event.payload.action === "attach" ? "Linked" : "Removed link to"} session ${event.payload.session_id}`;
  const changes = event.payload.changes as Partial<ProjectTicket> | undefined;
  if (changes?.completion_summary) return `Completed: ${changes.completion_summary}`;
  if (changes?.status) return `Moved to ${TICKET_STATUS_LABELS[changes.status]}`;
  return `Updated ${Object.keys(changes ?? {}).join(", ") || "the ticket"}`;
}

function TicketActivity({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useTicketEvents(projectId, ticketId);
  return (
    <section className="space-y-3">
      <h3 className="text-sm font-medium">Activity</h3>
      <TicketError error={query.error} />
      {query.isPending && <Spinner aria-label="Loading activity" />}
      <ol className="space-y-4">
        {query.data?.pages
          .flatMap((page) => page.data)
          .map((event) => (
            <li key={event.id} className="space-y-1 text-sm">
              <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                {event.session_id ? (
                  <SessionLink sessionId={event.session_id} />
                ) : (
                  <span>{event.user_id ?? "You"}</span>
                )}
                {event.agent_id && <span>Agent {event.agent_id.slice(0, 8)}</span>}
                <time dateTime={new Date(event.created_at * 1000).toISOString()}>
                  {new Date(event.created_at * 1000).toLocaleString()}
                </time>
              </div>
              <p className="whitespace-pre-wrap wrap-anywhere">{activityText(event)}</p>
            </li>
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
