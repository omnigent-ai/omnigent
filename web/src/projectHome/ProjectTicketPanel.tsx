import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  CheckIcon,
  ChevronRightIcon,
  ExternalLinkIcon,
  GitPullRequestIcon,
  LinkIcon,
  Maximize2Icon,
  Minimize2Icon,
  PencilIcon,
  PlusIcon,
  TagsIcon,
  UserRoundIcon,
  XIcon,
} from "lucide-react";
import { ProseMarkdown } from "@/components/blocks/ChatMarkdown";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
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
import { useViewerId } from "@/hooks/useViewerId";
import { markdownPreviewText } from "@/lib/markdownPreviewText";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { Link, useSearchParams, useReplaceSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { getProject, initializeProjectOrchestrator } from "@/lib/projectsApi";
import {
  createTicket,
  ticketOutputs,
  ticketEventStage,
  type ProjectTicket,
  type TicketEvent,
  type TicketOutput,
  type TicketTag,
} from "@/lib/projectTicketsApi";
import { ProjectSessionPanel } from "./ProjectSessionPanel";
import { ticketPanelSearch } from "./projectHomeNavigation";
import { TicketAgents } from "./TicketAgents";
import { TicketSessionLink } from "./TicketSessionLink";
import {
  CompleteTicketDialog,
  ReviewTicketDialog,
  RequestTicketChangesDialog,
  TicketError,
  TicketStageIcon,
  TicketTags,
} from "./TicketControls";
import { TicketRelatedTickets } from "./TicketRelatedTickets";
import {
  useTicket,
  useTicketActions,
  useTicketEvents,
  useTicketProject,
  useTicketLifecycleEnabled,
  useTicketStages,
} from "./useProjectTickets";

interface TicketExpansionProps {
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
}

function Panel({
  title,
  onClose,
  children,
  footer,
  expanded = false,
  onExpandedChange,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
} & TicketExpansionProps) {
  const expansionLabel = expanded ? "Restore ticket panel" : "Expand ticket";
  return (
    <section
      aria-label={title}
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[16px] border bg-background"
    >
      <header className="flex h-11 shrink-0 items-center gap-2 border-b border-border/60 pr-1.5 pl-4">
        <h2 title={title} className="min-w-0 flex-1 truncate text-ui font-medium">
          {title}
        </h2>
        {onExpandedChange && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="text-muted-foreground"
                aria-label={expansionLabel}
                onClick={() => onExpandedChange(!expanded)}
              >
                {expanded ? <Minimize2Icon aria-hidden /> : <Maximize2Icon aria-hidden />}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">{expansionLabel}</TooltipContent>
          </Tooltip>
        )}
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
  ticket,
}: {
  projectName: string;
  onClose: () => void;
  ticket?: { id: string; title: string };
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
      <ProjectSessionPanel
        sessionId={project.data.orchestrator_session_id}
        onClose={onClose}
        ticket={ticket}
      />
    );
  return (
    <Panel title="Orchestrator" onClose={onClose}>
      {ticket && (
        <Button asChild variant="ghost" size="sm">
          <Link to={ticketPanelSearch(ticket.id)}>Back to ticket</Link>
        </Button>
      )}
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
  expanded,
  onExpandedChange,
}: {
  projectName: string;
  ticketId: string;
  onClose: () => void;
  onCreated: (ticketId: string) => void;
} & TicketExpansionProps) {
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
      <Panel
        title="New ticket"
        onClose={onClose}
        expanded={expanded}
        onExpandedChange={onExpandedChange}
      >
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
      <Panel
        title="Ticket"
        onClose={onClose}
        expanded={expanded}
        onExpandedChange={onExpandedChange}
      >
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
      projectName={projectName}
      ticketId={ticketId}
      onClose={onClose}
      expanded={expanded}
      onExpandedChange={onExpandedChange}
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
  projectName,
  ticketId,
  onClose,
  expanded,
  onExpandedChange,
}: {
  projectId: string;
  projectName: string;
  ticketId: string;
  onClose: () => void;
} & TicketExpansionProps) {
  const query = useTicket(projectId, ticketId);
  const { update, comment } = useTicketActions(projectId);
  const [editing, setEditing] = useState(false);
  const [completing, setCompleting] = useState<{ stageId?: string; approving: boolean } | null>(
    null,
  );
  const [reviewing, setReviewing] = useState(false);
  const [requestingChanges, setRequestingChanges] = useState(false);
  const lifecycle = useTicketLifecycleEnabled();
  const configuration = useTicketStages(projectId);
  const stages = configuration.data?.stages ?? [];
  const [searchParams] = useSearchParams();
  const replaceSearchParams = useReplaceSearchParams();
  const chatSessionId = searchParams.get("session");
  const orchestratorChat = searchParams.has("orchestrator");
  const chatting = !!chatSessionId || orchestratorChat;
  const closeChat = () => replaceSearchParams(new URLSearchParams({ ticket: ticketId }));
  const ticket = query.data;
  if (!ticket)
    return (
      <Panel
        title="Ticket"
        onClose={onClose}
        expanded={expanded}
        onExpandedChange={onExpandedChange}
      >
        {query.isPending && <Spinner aria-label="Loading ticket" />}
        <TicketError error={query.error} />
        {query.isError && (
          <Button variant="ghost" onClick={() => void query.refetch()}>
            Try again
          </Button>
        )}
      </Panel>
    );
  const currentStage = stages.find((stage) => stage.id === ticket.stage_id);
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div
        hidden={chatting}
        className={chatting ? "hidden" : "flex min-h-0 min-w-0 flex-1 flex-col"}
      >
        <Panel
          title={ticket.title}
          onClose={onClose}
          expanded={expanded}
          onExpandedChange={onExpandedChange}
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
                  value={ticket.stage_id}
                  disabled={update.isPending || configuration.isPending || configuration.isError}
                  onValueChange={(value) => {
                    update.reset();
                    const stage = stages.find((candidate) => candidate.id === value);
                    if (!stage) return;
                    if (!configuration.supported && stage.is_complete)
                      setCompleting({ stageId: stage.id, approving: false });
                    else if (!configuration.supported && value === "review") setReviewing(true);
                    else update.mutate({ id: ticketId, changes: { stage_id: value } });
                  }}
                >
                  <SelectTrigger
                    size="sm"
                    aria-label="Ticket stage"
                    className="shrink-0 border-border/60 bg-muted/30"
                  >
                    <SelectValue placeholder={ticket.stage_id} />
                  </SelectTrigger>
                  <SelectContent>
                    {stages.map((stage) => (
                      <SelectItem key={stage.id} value={stage.id}>
                        <TicketStageIcon stage={stage} />
                        {stage.name}
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
              <TicketError error={configuration.error} />
              {currentStage?.instructions && (
                <p className="text-sm text-muted-foreground">{currentStage.instructions}</p>
              )}
              <TicketAgents projectId={projectId} ticket={ticket} />
              {ticket.description ? (
                <ProseMarkdown className="text-ui leading-relaxed">
                  {ticket.description}
                </ProseMarkdown>
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
          {!editing && !completing && !reviewing && !requestingChanges && (
            <TicketError error={update.error} />
          )}
          {(lifecycle || ticket.review_summary || ticket.completion_summary) && (
            <div className="space-y-2">
              {lifecycle && <TicketPlan projectId={projectId} ticket={ticket} />}
              {ticket.review_summary && (
                <TicketDisclosure
                  label="Human review"
                  title="Review brief"
                  preview={markdownPreviewText(ticket.review_summary ?? "")}
                  action={
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      aria-label="Edit review brief"
                      onClick={() => {
                        update.reset();
                        setReviewing(true);
                      }}
                    >
                      <PencilIcon aria-hidden />
                    </Button>
                  }
                  footer={
                    <div className="flex flex-wrap gap-2">
                      <Button
                        size="sm"
                        disabled={
                          update.isPending ||
                          currentStage?.is_complete ||
                          !stages.some((stage) => stage.is_complete)
                        }
                        onClick={() => {
                          update.reset();
                          setCompleting({ approving: true });
                        }}
                      >
                        <CheckIcon aria-hidden />
                        Approve &amp; complete
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={update.isPending}
                        onClick={() => {
                          update.reset();
                          setRequestingChanges(true);
                        }}
                      >
                        Request changes
                      </Button>
                    </div>
                  }
                >
                  <ProseMarkdown className="text-ui leading-relaxed">
                    {ticket.review_summary ?? ""}
                  </ProseMarkdown>
                  <p className="mt-3 text-sm text-muted-foreground">
                    These actions update the ticket, not agent prompts. Needs response stays
                    unchanged; resolve it separately when its request is answered.
                  </p>
                </TicketDisclosure>
              )}
              {lifecycle && !ticket.review_summary && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="-ml-2 text-muted-foreground"
                  onClick={() => {
                    update.reset();
                    setReviewing(true);
                  }}
                >
                  <PlusIcon aria-hidden /> Request review
                </Button>
              )}
              {ticket.completion_summary && (
                <TicketDisclosure
                  title="Completion summary"
                  icon={<CheckIcon aria-hidden className="size-4 text-success" />}
                  preview={markdownPreviewText(ticket.completion_summary)}
                >
                  <ProseMarkdown className="text-ui">{ticket.completion_summary}</ProseMarkdown>
                </TicketDisclosure>
              )}
            </div>
          )}
          <TicketOutputs projectId={projectId} ticketId={ticketId} />
          {lifecycle && <TicketRelatedTickets projectId={projectId} ticketId={ticketId} />}
          <TicketActivity projectId={projectId} ticketId={ticketId} />
          {completing && (
            <CompleteTicketDialog
              title={ticket.title}
              approving={completing.approving}
              stages={stages}
              initialStageId={completing.stageId}
              required={!configuration.supported}
              pending={update.isPending}
              error={update.error}
              onClose={() => {
                setCompleting(null);
                update.reset();
              }}
              onComplete={(summary, stageId) =>
                update.mutate(
                  {
                    id: ticketId,
                    changes: {
                      stage_id: stageId,
                      ...(summary ? { completion_summary: summary } : {}),
                      ...(completing.approving ? { comment: "Accepted the ticket review." } : {}),
                    },
                  },
                  { onSuccess: () => setCompleting(null) },
                )
              }
            />
          )}
          {reviewing && (
            <ReviewTicketDialog
              title={ticket.title}
              initialSummary={ticket.review_summary}
              pending={update.isPending}
              error={update.error}
              onClose={() => {
                setReviewing(false);
                update.reset();
              }}
              onComplete={(summary) =>
                update.mutate(
                  {
                    id: ticketId,
                    changes: {
                      ...(!configuration.supported ? { stage_id: "review" } : {}),
                      review_summary: summary,
                      tags: ticket.tags.some((tag) => tag.key === "needs-human")
                        ? ticket.tags
                        : [...ticket.tags, { key: "needs-human" }],
                    },
                  },
                  { onSuccess: () => setReviewing(false) },
                )
              }
            />
          )}
          {requestingChanges && (
            <RequestTicketChangesDialog
              title={ticket.title}
              pending={update.isPending}
              error={update.error}
              onClose={() => {
                setRequestingChanges(false);
                update.reset();
              }}
              onComplete={(feedback) =>
                update.mutate(
                  {
                    id: ticketId,
                    changes: {
                      ...(!configuration.supported ? { stage_id: "in_progress" } : {}),
                      comment: feedback,
                    },
                  },
                  { onSuccess: () => setRequestingChanges(false) },
                )
              }
            />
          )}
        </Panel>
      </div>
      {chatSessionId ? (
        <ProjectSessionPanel sessionId={chatSessionId} onClose={closeChat} ticket={ticket} />
      ) : (
        orchestratorChat && (
          <ProjectOrchestratorPanel projectName={projectName} onClose={closeChat} ticket={ticket} />
        )
      )}
    </div>
  );
}

function TicketDisclosure({
  title,
  label = title,
  preview,
  icon,
  action,
  children,
  footer,
  open,
  onOpenChange,
}: {
  title: string;
  label?: string;
  preview: string;
  icon?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} asChild>
      <section aria-label={label} className="rounded-lg border border-border/60">
        <div className="flex items-center gap-1 pr-1.5">
          <h3 className="min-w-0 flex-1">
            <CollapsibleTrigger className="group flex w-full min-w-0 items-center gap-3 rounded-md p-3 text-left outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring">
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2 text-sm font-medium">
                  {icon}
                  {title}
                </span>
                {preview && (
                  <span
                    aria-hidden
                    className="mt-0.5 block truncate text-xs font-normal text-muted-foreground group-data-[state=open]:hidden"
                  >
                    {preview}
                  </span>
                )}
              </span>
              <ChevronRightIcon
                aria-hidden
                className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90"
              />
            </CollapsibleTrigger>
          </h3>
          {action}
        </div>
        <CollapsibleContent className="px-3 pb-3">{children}</CollapsibleContent>
        {footer && <div className="px-3 pb-3">{footer}</div>}
      </section>
    </Collapsible>
  );
}

function TicketPlan({ projectId, ticket }: { projectId: string; ticket: ProjectTicket }) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [plan, setPlan] = useState("");
  const { update } = useTicketActions(projectId);
  const edit = () => {
    setPlan(ticket.plan ?? "");
    update.reset();
    setEditing(true);
    setOpen(true);
  };
  if (!editing && !ticket.plan)
    return (
      <Button variant="ghost" size="sm" className="-ml-2 text-muted-foreground" onClick={edit}>
        <PlusIcon aria-hidden />
        Add plan
      </Button>
    );
  return (
    <TicketDisclosure
      label="Ticket plan"
      title="Plan"
      preview={
        editing
          ? "Unsaved changes"
          : markdownPreviewText(ticket.plan ?? "") || "Approach and acceptance criteria"
      }
      open={open}
      onOpenChange={setOpen}
      action={
        !editing && (
          <Button variant="ghost" size="icon-sm" aria-label="Edit plan" onClick={edit}>
            <PencilIcon aria-hidden />
          </Button>
        )
      }
    >
      {editing ? (
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            update.mutate(
              { id: ticket.id, changes: { plan: plan.trim() } },
              { onSuccess: () => setEditing(false), onError: () => setOpen(true) },
            );
          }}
        >
          <Textarea
            aria-label="Plan"
            rows={7}
            maxLength={3000}
            value={plan}
            disabled={update.isPending}
            onChange={(event) => setPlan(event.target.value)}
            placeholder="Approach, acceptance criteria, and decisions still needed…"
          />
          <TicketError error={update.error} />
          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={update.isPending}
              onClick={() => setEditing(false)}
            >
              Cancel
            </Button>
            <Button type="submit" size="sm" loading={update.isPending}>
              Save plan
            </Button>
          </div>
        </form>
      ) : ticket.plan ? (
        <ProseMarkdown className="text-ui leading-relaxed">{ticket.plan}</ProseMarkdown>
      ) : (
        <p className="text-sm text-muted-foreground">
          Align on the approach and what counts as done in the agent chat. Save the agreed plan
          here.
        </p>
      )}
    </TicketDisclosure>
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
      <p className="text-xs text-muted-foreground">
        For a conversation or an approval prompt, open an agent’s chat above.
      </p>
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

function ActivityChange({ event }: { event: TicketEvent }) {
  if (event.kind === "created") return <>created this ticket</>;
  if (event.kind === "output") {
    const output = ticketOutputs([event])[0];
    return <>added {output ? <TicketOutputLink output={output} inline /> : "an output"}</>;
  }
  if (typeof event.payload.session_id === "string")
    return (
      <>
        {event.payload.action === "attach"
          ? "linked"
          : event.payload.action === "updated"
            ? event.payload.involvement
              ? "updated involvement for"
              : "cleared involvement for"
            : "unlinked"}{" "}
        <TicketSessionLink sessionId={event.payload.session_id} ticketId={event.ticket_id} />
        {typeof event.payload.involvement === "string" && event.payload.involvement && (
          <> — {event.payload.involvement}</>
        )}
      </>
    );
  if (typeof event.payload.related_ticket_id === "string")
    return (
      <>
        {event.payload.action === "attach"
          ? "linked a related ticket"
          : "unlinked a related ticket"}{" "}
        <Link
          to={ticketPanelSearch(event.payload.related_ticket_id)}
          className="text-foreground hover:underline"
        >
          View ticket
        </Link>
      </>
    );
  const changes = event.payload.changes as Partial<ProjectTicket> | undefined;
  const stage = ticketEventStage(event);
  if (stage)
    return (
      <>
        moved to <span className="font-medium text-foreground">{stage.name}</span>
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
    typeof event.payload.text === "string"
      ? event.payload.text
      : (changes?.completion_summary ?? changes?.review_summary);
  const agent = !!(event.session_id || event.agent_id);
  const author =
    event.user_id && event.user_id === viewerId
      ? "You"
      : (event.user_id ?? (agent ? "Agent" : "System"));
  const actor = event.session_id ? (
    <TicketSessionLink sessionId={event.session_id} ticketId={event.ticket_id} />
  ) : (
    <span className="text-foreground">{author}</span>
  );
  const stage = ticketEventStage(event);
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
        ) : stage ? (
          <TicketStageIcon stage={stage} />
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
