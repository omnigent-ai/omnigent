import { Fragment, useRef, useState, type KeyboardEvent, type ReactNode, type Ref } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  ExternalLinkIcon,
  FileIcon,
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
import { useViewerId } from "@/hooks/useViewerId";
import { isCommandEnterSubmitKey } from "@/lib/hotkeys";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { Link, useSearchParams, useReplaceSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { WorkspaceFileIcon } from "@/shell/WorkspaceFileIcon";
import { ProjectOrchestrator } from "./ProjectOrchestrator";
import {
  createTicket,
  getProjectFile,
  getTicket,
  openProjectFile,
  requesterLabel,
  ticketOutputs,
  ticketEventStage,
  type ProjectTicket,
  type TicketEvent,
  type TicketFinalUpdate,
  type TicketOutput,
  type TicketLabel,
} from "@/lib/projectTicketsApi";
import { getCurrentUserId } from "@/lib/identity";
import { ProjectSessionPanel } from "./ProjectSessionPanel";
import { TICKET_ASK_PARAM, ticketPanelSearch } from "./projectHomeNavigation";
import { TicketAgents } from "./TicketAgents";
import { TicketCopyLinkButton } from "./TicketCopyLinkButton";
import { TicketCostSection } from "./ProjectCost";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { SessionImage } from "@/components/SessionImage";
import { MentionTextarea } from "./MentionTextarea";
import { usePersonaForSession, useProjectPersonaLookup } from "./usePersonaSessions";
import { payloadMentions } from "./personaMentions";
import { TicketCommentBody } from "./TicketCommentBody";
import { TicketFileLinks } from "./TicketFileLinks";
import { TicketImageAttachButton, TicketMarkdown, useTicketImageUpload } from "./ticketImages";
import { TicketSessionLink } from "./TicketSessionLink";
import { TicketError, TicketStageIcon, TicketLabels } from "./TicketControls";
import { TicketRelatedTickets } from "./TicketRelatedTickets";
import { parseAskPosition, useTicketAsk } from "./useTicketAsk";
import { useTicketSeen } from "./useTicketSeen";
import {
  newTicketDraftKey,
  ticketCommentDraftKey,
  useTicketDraft,
  writeTicketDraft,
} from "./ticketTextDrafts";
import { useProjectAgentSession } from "./useProjectAgentSession";
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
  actions,
  expanded = false,
  onExpandedChange,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** Header buttons placed before expand and close. */
  actions?: ReactNode;
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
        {actions}
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
  ticket,
}: {
  projectName: string;
  onClose: () => void;
  ticket?: { id: string; title: string };
}) {
  return <ProjectOrchestrator projectName={projectName} ticket={ticket} />;
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
      writeTicketDraft(newTicketDraftKey(projectName, "title"), "");
      writeTicketDraft(newTicketDraftKey(projectName, "description"), "");
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
          key={projectName}
          draftProjectName={projectName}
          resolveProjectId={async () => projectId ?? (await ensure.mutateAsync())}
          onSave={(body) => create.mutate(body)}
          pending={create.isPending}
          error={create.error}
          disabled={projects.isLoading || projects.isError}
        />
        <TicketError error={projects.error} />
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

/** Mod+Enter submits through `requestSubmit`, so native validation and `onSubmit` guards still apply. */
function submitOnCommandEnter(event: KeyboardEvent<HTMLFormElement>) {
  if (!isCommandEnterSubmitKey(event.nativeEvent)) return;
  event.preventDefault();
  event.stopPropagation();
  event.currentTarget.requestSubmit();
}

function TicketEditor({
  ticket,
  onSave,
  onCancel,
  pending,
  error,
  disabled = false,
  resolveProjectId,
  draftProjectName,
}: {
  resolveProjectId: () => Promise<string>;
  /** Persist the unsaved form as this project's new-ticket draft. */
  draftProjectName?: string;
  ticket?: ProjectTicket;
  onSave: (body: { title: string; description: string }) => void;
  onCancel?: () => void;
  pending: boolean;
  error: Error | null;
  disabled?: boolean;
}) {
  const [title, setTitle] = useTicketDraft(
    draftProjectName && newTicketDraftKey(draftProjectName, "title"),
    ticket?.title ?? "",
  );
  const [description, setDescription] = useTicketDraft(
    draftProjectName && newTicketDraftKey(draftProjectName, "description"),
    ticket?.description ?? "",
  );
  const mobile = useIsMobileViewport();
  const images = useTicketImageUpload(resolveProjectId, setDescription);
  return (
    <form
      className="space-y-4"
      onKeyDown={submitOnCommandEnter}
      onSubmit={(event) => {
        event.preventDefault();
        if (!title.trim() || pending || disabled || images.uploading) return;
        onSave({ title: title.trim(), description });
      }}
    >
      <label className="grid gap-2 text-sm">
        Title
        <Input
          autoFocus={!mobile}
          name="title"
          className="focus-visible:ring-0"
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
          className="focus-visible:ring-0"
          rows={6}
          maxLength={3000}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          onPaste={images.onPaste}
          onDragOver={images.onDragOver}
          onDrop={images.onDrop}
          placeholder="What should change? Done when…"
          disabled={pending}
        />
      </label>
      {images.uploading && <p className="text-xs text-muted-foreground">Uploading image…</p>}
      <TicketError error={images.error} />
      <TicketError error={error} />
      <div className="flex justify-end gap-2">
        <TicketImageAttachButton onFiles={images.attach} disabled={pending} />
        <span className="flex-1" />
        {onCancel && (
          <Button type="button" variant="ghost" disabled={pending} onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button
          type="submit"
          loading={pending}
          disabled={disabled || images.uploading || !title.trim()}
        >
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
  // File paths in text without an authoring session resolve against the project's agent.
  const projectSessionId = useProjectAgentSession(projectId).sessionId;
  const { update, comment } = useTicketActions(projectId);
  const [editing, setEditing] = useState(false);
  const lifecycle = useTicketLifecycleEnabled();
  const configuration = useTicketStages(projectId);
  const stages = configuration.data?.stages ?? [];
  const [searchParams] = useSearchParams();
  const replaceSearchParams = useReplaceSearchParams();
  const chatSessionId = searchParams.get("session");
  const orchestratorChat =
    searchParams.get("view") === "chat" ||
    searchParams.has("orchestrator") ||
    searchParams.get("agent") === "primary";
  const chatting = !!chatSessionId || orchestratorChat;
  const closeChat = () => {
    const next = new URLSearchParams(ticketPanelSearch(ticketId, searchParams));
    if (searchParams.get("view") === "chat" || searchParams.get("agent") === "primary")
      next.set("view", "board");
    replaceSearchParams(next);
  };
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
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div
        hidden={chatting}
        className={chatting ? "hidden" : "flex min-h-0 min-w-0 flex-1 flex-col"}
      >
        <Panel
          title={ticket.title}
          actions={<TicketCopyLinkButton projectName={projectName} ticketId={ticketId} />}
          onClose={onClose}
          expanded={expanded}
          onExpandedChange={onExpandedChange}
          footer={
            <TicketCommentComposer projectId={projectId} ticketId={ticketId} comment={comment} />
          }
        >
          {editing ? (
            <TicketEditor
              ticket={ticket}
              resolveProjectId={async () => projectId}
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
              <TicketSignOffs
                ticket={ticket}
                pending={update.isPending || comment.isPending}
                onApprove={(label) => {
                  update.reset();
                  // Labels replace the whole list: build on a fresh read, and only while the
                  // ticket still asks for this sign-off, so a newer edit isn't undone.
                  void getTicket(projectId, ticketId)
                    .then((fresh) => {
                      const stillOffered = (fresh.sign_offs ?? []).some(
                        (offer) =>
                          offer.key === label.key &&
                          (offer.value ?? null) === (label.value ?? null),
                      );
                      if (!stillOffered) return void query.refetch();
                      update.mutate({
                        id: ticketId,
                        changes: { labels: [...fresh.labels, label] },
                      });
                    })
                    .catch(() => void query.refetch());
                }}
                onDecline={(label) =>
                  comment.mutate({
                    id: ticketId,
                    body: { text: `Not adding \`${label.key}\` yet.` },
                  })
                }
              />
              <div className="flex flex-wrap items-center gap-2">
                <Select
                  value={ticket.stage_id}
                  disabled={update.isPending || configuration.isPending || configuration.isError}
                  onValueChange={(value) => {
                    update.reset();
                    const stage = stages.find((candidate) => candidate.id === value);
                    if (!stage) return;
                    update.mutate({ id: ticketId, changes: { stage_id: value } });
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
                {ticket.labels.length > 0 && <TicketLabels labels={ticket.labels} />}
                <LabelEditor
                  labels={ticket.labels}
                  disabled={update.isPending}
                  error={update.error}
                  onChange={(labels) => update.mutate({ id: ticketId, changes: { labels } })}
                />
              </div>
              <TicketError error={configuration.error} />
              {ticket.requested_by && (
                <p className="text-sm text-muted-foreground" data-testid="ticket-panel-requester">
                  Requested by {requesterLabel(ticket.requested_by, getCurrentUserId())}
                </p>
              )}
              <TicketAgents projectId={projectId} projectName={projectName} ticket={ticket} />
              <TicketCostSection projectId={projectId} ticketId={ticketId} />
              <section aria-label="Description" className="space-y-2">
                <h3 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Description</h3>
                {ticket.description ? (
                  <TicketFileLinks sessionId={projectSessionId}>
                    <TicketMarkdown className="text-ui leading-relaxed">
                      {ticket.description}
                    </TicketMarkdown>
                  </TicketFileLinks>
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
              </section>
              {ticket.final_update && (
                <TicketFinalUpdateSection
                  update={ticket.final_update}
                  fallbackSessionId={projectSessionId}
                />
              )}
            </div>
          )}
          {!editing && <TicketError error={update.error} />}
          <TicketOutputs projectId={projectId} ticketId={ticketId} />
          {lifecycle && <TicketRelatedTickets projectId={projectId} ticketId={ticketId} />}
          <TicketActivity key={ticket.id} projectId={projectId} ticket={ticket} />
        </Panel>
      </div>
      {chatSessionId ? (
        <ProjectSessionPanel sessionId={chatSessionId} />
      ) : (
        orchestratorChat && (
          <ProjectOrchestratorPanel projectName={projectName} onClose={closeChat} ticket={ticket} />
        )
      )}
    </div>
  );
}

/** One-click answers to an ask waiting on the viewer's sign-off, such as `risk-ok`. */
function TicketSignOffs({
  ticket,
  pending,
  onApprove,
  onDecline,
}: {
  ticket: ProjectTicket;
  pending: boolean;
  onApprove: (label: TicketLabel) => void;
  onDecline: (label: TicketLabel) => void;
}) {
  const offers = ticket.sign_offs ?? [];
  if (offers.length === 0) return null;
  return (
    <section
      aria-label="Your sign-off"
      className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3"
    >
      <p className="text-sm font-medium">This ticket is waiting on your sign-off.</p>
      {offers.map((label) => (
        <div key={label.key} className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={pending} onClick={() => onApprove(label)}>
            Approve: add {label.key}
          </Button>
          <Button size="sm" variant="ghost" disabled={pending} onClick={() => onDecline(label)}>
            Decline
          </Button>
        </div>
      ))}
    </section>
  );
}

function TicketCommentComposer({
  projectId,
  ticketId,
  comment,
}: {
  projectId: string;
  ticketId: string;
  comment: ReturnType<typeof useTicketActions>["comment"];
}) {
  const draftKey = ticketCommentDraftKey(projectId, ticketId);
  const [text, setText] = useTicketDraft(draftKey);
  const [attaching, setAttaching] = useState(false);
  const [outputUrl, setOutputUrl] = useState("");
  const [outputType, setOutputType] = useState<TicketOutput["type"]>("pull_request");
  const images = useTicketImageUpload(async () => projectId, setText);
  return (
    <form
      className="space-y-2"
      onKeyDown={submitOnCommandEnter}
      onSubmit={(event) => {
        event.preventDefault();
        if (!text.trim() || comment.isPending || images.uploading) return;
        // The promise settles even if the user navigates away, so the posted draft still clears.
        comment
          .mutateAsync({
            id: ticketId,
            body: {
              text: text.trim(),
              outputs:
                attaching && outputUrl.trim() ? [{ type: outputType, url: outputUrl.trim() }] : [],
            },
          })
          .then(
            () => {
              writeTicketDraft(draftKey, "");
              setText("");
              setOutputUrl("");
              setAttaching(false);
            },
            () => {}, // Shown through comment.error.
          );
      }}
    >
      <div className="rounded-xl border border-input bg-background focus-within:border-ring">
        <MentionTextarea
          projectId={projectId}
          name="comment"
          aria-label="Add a comment"
          required
          maxLength={2000}
          rows={2}
          className="max-h-36 min-h-16 resize-none rounded-none border-0 px-3 shadow-none focus-visible:ring-0"
          value={text}
          disabled={comment.isPending}
          onValueChange={setText}
          onPaste={images.onPaste}
          onDragOver={images.onDragOver}
          onDrop={images.onDrop}
          placeholder="Add a comment… type @ to cc a persona"
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
              className="min-w-36 flex-1 focus-visible:ring-0"
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
          <div className="flex items-center gap-1">
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
            <TicketImageAttachButton onFiles={images.attach} disabled={comment.isPending} />
          </div>
          <Button
            type="submit"
            size="sm"
            loading={comment.isPending}
            disabled={!text.trim() || images.uploading}
          >
            Comment
          </Button>
        </div>
      </div>
      {images.uploading && <p className="text-xs text-muted-foreground">Uploading image…</p>}
      <TicketError error={images.error} />
      <TicketError error={comment.error} />
      <span role="status" className="sr-only">
        {comment.isSuccess ? "Comment added" : ""}
      </span>
    </form>
  );
}

function LabelEditor({
  labels,
  disabled,
  error,
  onChange,
}: {
  labels: TicketLabel[];
  disabled: boolean;
  error: Error | null;
  onChange: (labels: TicketLabel[]) => void;
}) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size={labels.length ? "icon-sm" : "sm"}
          className="text-muted-foreground"
          aria-label={labels.length ? "Edit labels" : "Add labels"}
        >
          <TagsIcon aria-hidden />
          {labels.length === 0 && "Labels"}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        aria-label="Edit labels"
        className="max-w-[calc(100vw-2rem)] gap-3 p-3"
      >
        <h3 className="text-sm font-medium">Labels</h3>
        {labels.length > 0 && (
          <ul className="max-h-48 space-y-1 overflow-y-auto">
            {labels.map((label) => (
              <li key={label.key} className="flex min-w-0 items-center gap-2">
                <span className="min-w-0 flex-1 text-sm wrap-anywhere">
                  {label.key}
                  {label.value && <span className="text-muted-foreground">: {label.value}</span>}
                </span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  disabled={disabled}
                  aria-label={`Remove label ${label.key}`}
                  onClick={() => onChange(labels.filter((item) => item.key !== label.key))}
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
            onChange([...labels.filter((label) => label.key !== next.key), next]);
          }}
        >
          <Input
            name="label_key"
            className="focus-visible:ring-0"
            autoComplete="off"
            aria-label="Label key"
            placeholder="Key, e.g. needs-human…"
            maxLength={64}
            required
            disabled={disabled}
            value={key}
            onChange={(event) => setKey(event.target.value)}
          />
          <Input
            name="label_value"
            className="focus-visible:ring-0"
            autoComplete="off"
            aria-label="Label value"
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
              (labels.length >= 20 && !labels.some((label) => label.key === key.trim()))
            }
          >
            <PlusIcon aria-hidden />
            Save label
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
    <div className="space-y-4">
      {(
        [
          ["pull_request", "Pull requests"],
          ["artifact", "Files"],
          ["link", "Links"],
        ] as const
      ).map(([type, label]) => {
        const group = outputs.filter((output) => output.type === type);
        return group.length > 0 ? (
          <section key={type} aria-label={label} className="space-y-2">
            <h3 className="text-[length:calc(var(--text-ui)+2px)] font-bold">{label}</h3>
            <ul className="divide-y divide-border/60 overflow-hidden rounded-lg border border-border/60">
              {group.map((output) => (
                <li key={output.url}>
                  <TicketOutputLink output={output} />
                  <TicketArtifactImage output={output} className="px-3 pb-3" />
                </li>
              ))}
            </ul>
          </section>
        ) : null;
      })}
      <TicketError error={query.error} />
      {query.hasNextPage && (
        <Button
          variant="ghost"
          size="sm"
          loading={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          More attachments
        </Button>
      )}
    </div>
  );
}

function useProjectFileInfo(url: string) {
  return useQuery({
    queryKey: ["project-file", url],
    queryFn: () => getProjectFile(url),
    staleTime: Infinity,
  });
}

// Raster types the content route serves inline; SVG stays a download.
const PREVIEW_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

/** Inline preview for an image project file, such as a screenshot of a change working. */
function TicketArtifactImage({ output, className }: { output: TicketOutput; className?: string }) {
  const file = useProjectFileInfo(output.url);
  const type = file.data?.content_type?.split(";", 1)[0].trim().toLowerCase();
  if (output.type !== "artifact" || !type || !PREVIEW_IMAGE_TYPES.has(type)) return null;
  return (
    <div className={cn("w-fit max-w-full", className)}>
      <SessionImage
        path={output.url}
        alt={file.data?.title || file.data?.filename || "Screenshot"}
      />
    </div>
  );
}

/** The activity timeline's icon for an attached project file, by its type. */
function TicketArtifactEventIcon({ output }: { output: TicketOutput }) {
  const file = useProjectFileInfo(output.url);
  return file.data ? (
    <WorkspaceFileIcon path={file.data.filename} mimeType={file.data.content_type} />
  ) : (
    <FileIcon aria-hidden className="size-3.5" />
  );
}

function TicketArtifactLink({ output, inline }: { output: TicketOutput; inline: boolean }) {
  const file = useProjectFileInfo(output.url);
  const [error, setError] = useState<string | null>(null);
  const name = file.data?.title ?? file.data?.filename ?? "Project file";
  return (
    <button
      type="button"
      title={name}
      onClick={() => {
        setError(null);
        openProjectFile(output.url, file.data?.filename ?? "file").catch((reason: unknown) =>
          setError(reason instanceof Error ? reason.message : "Couldn't open the file"),
        );
      }}
      className={cn(
        "min-w-0 rounded-sm text-left text-foreground underline-offset-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        inline
          ? "wrap-anywhere hover:underline"
          : "flex w-full items-center gap-3 p-3 transition-colors hover:bg-muted/40 focus-visible:ring-inset",
      )}
    >
      {!inline &&
        (file.data ? (
          <WorkspaceFileIcon
            path={file.data.filename}
            mimeType={file.data.content_type}
            className="size-4"
          />
        ) : (
          <FileIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        ))}
      <span className={inline ? undefined : "min-w-0 flex-1"}>
        <span className={inline ? undefined : "block truncate text-sm font-medium"}>{name}</span>
        {!inline && (
          <span className="block text-sm text-muted-foreground">
            {error ??
              (file.data ? `Project file · ${formatBytes(file.data.bytes)}` : "Project file")}
          </span>
        )}
      </span>
    </button>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function TicketOutputLink({ output, inline = false }: { output: TicketOutput; inline?: boolean }) {
  if (output.type === "artifact") return <TicketArtifactLink output={output} inline={inline} />;
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
              ? "updated the role for"
              : "cleared the role for"
            : "unlinked"}{" "}
        <TicketSessionLink sessionId={event.payload.session_id} />
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
  if (event.payload.cleared_by_comment === true) return <>cleared Needs response by commenting</>;
  if (typeof event.payload.cleared_by_label === "string")
    return <>cleared Needs response by adding {event.payload.cleared_by_label}</>;
  const changes = event.payload.changes as Partial<ProjectTicket> | undefined;
  const stage = ticketEventStage(event);
  const fields = Object.keys(changes ?? {})
    .filter((field) => !stage || (field !== "stage_id" && field !== "status"))
    .map((field) => field.replaceAll("_", " "));
  return (
    <>
      {stage && (
        <>
          moved to <span className="font-medium text-foreground">{stage.name}</span>
        </>
      )}
      {fields.length > 0 && (
        <>
          {stage && "; "}
          updated {fields.join(", ")}
        </>
      )}
      {!stage && !fields.length && "updated the ticket"}
    </>
  );
}

function TicketActivity({ projectId, ticket }: { projectId: string; ticket: ProjectTicket }) {
  const query = useTicketEvents(projectId, ticket.id);
  const now = useNow();
  const viewerId = useViewerId();
  const projectSessionId = useProjectAgentSession(projectId).sessionId;
  const personaLookup = useProjectPersonaLookup(projectId);
  const events = query.data?.pages.flatMap((page) => page.data) ?? [];
  const [searchParams] = useSearchParams();
  // Read once: the question the viewer came from the Inbox to answer.
  const [askPosition] = useState(() => parseAskPosition(searchParams.get(TICKET_ASK_PARAM)));
  const activityRef = useRef<HTMLElement | null>(null);
  const { dividerPosition, dividerRef } = useTicketSeen({
    projectId,
    ticket,
    events,
    hasNextPage: query.hasNextPage,
    isFetching: query.isFetching,
    fetchNextPage: query.fetchNextPage,
    scrollToNew: askPosition === null,
  });
  const askTarget = useTicketAsk({
    askPosition,
    events,
    hasNextPage: query.hasNextPage,
    isFetching: query.isFetching,
    fetchNextPage: query.fetchNextPage,
    containerRef: activityRef,
  });
  const [showAgentNotes, setShowAgentNotes] = useState(false);
  if (!query.isPending && !query.error && !query.hasNextPage && events.length === 0) return null;
  const entries = groupAgentNotes(events);
  const hasAgentNotes = entries.some((entry) => Array.isArray(entry));
  const renderItem = (event: TicketEvent) => (
    <TicketActivityItem
      key={event.id}
      event={event}
      personaLookup={personaLookup}
      nowMs={now.getTime()}
      viewerId={viewerId}
      fallbackSessionId={projectSessionId}
      highlighted={event.position === askTarget}
    />
  );
  return (
    <section
      ref={activityRef}
      aria-label="Ticket activity"
      className="space-y-4 border-t border-border/60 pt-5"
    >
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Activity</h3>
        {hasAgentNotes && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-xs text-muted-foreground"
            aria-pressed={showAgentNotes}
            onClick={() => setShowAgentNotes(!showAgentNotes)}
          >
            {showAgentNotes ? "Hide agent notes" : "Show agent notes"}
          </Button>
        )}
      </div>
      <TicketError error={query.error} />
      {query.isPending && <Spinner aria-label="Loading activity" />}
      <ol>
        {entries.map((entry) => {
          const group = Array.isArray(entry) ? entry : [entry];
          const divider =
            dividerPosition !== null &&
            group.some((event) => event.position === dividerPosition) ? (
              <TicketNewDivider dividerRef={dividerRef} />
            ) : null;
          return (
            <Fragment key={group[0].id}>
              {divider}
              {!Array.isArray(entry) || showAgentNotes ? (
                group.map(renderItem)
              ) : (
                <TicketAgentNotesFold notes={entry} renderItem={renderItem} />
              )}
            </Fragment>
          );
        })}
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

/** Agent notes are written for the coordinator; the human sees them folded by default. */
function isAgentNote(event: TicketEvent): boolean {
  return (
    (event.kind === "comment" || event.kind === "execution") && event.payload.audience === "agents"
  );
}

/** Activity in order, with each run of consecutive agent notes collected into one array. */
function groupAgentNotes(events: TicketEvent[]): (TicketEvent | TicketEvent[])[] {
  const entries: (TicketEvent | TicketEvent[])[] = [];
  for (const event of events) {
    const last = entries.at(-1);
    if (!isAgentNote(event)) entries.push(event);
    else if (Array.isArray(last)) last.push(event);
    else entries.push([event]);
  }
  return entries;
}

function TicketAgentNotesFold({
  notes,
  renderItem,
}: {
  notes: TicketEvent[];
  renderItem: (event: TicketEvent) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const label = `${notes.length} agent ${notes.length === 1 ? "note" : "notes"}`;
  return (
    <>
      <li className="relative flex min-w-0 gap-3 pb-4 last:pb-0 before:absolute before:top-6 before:bottom-0 before:left-3 before:w-px before:bg-border/60 last:before:hidden">
        <span className="relative flex size-6 shrink-0 items-center justify-center bg-background text-muted-foreground">
          <BotIcon aria-hidden className="size-3.5" />
        </span>
        <Button
          type="button"
          variant="link"
          size="sm"
          className="h-6 px-0 text-sm text-muted-foreground hover:text-foreground"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {open ? `Hide ${label}` : label}
        </Button>
      </li>
      {open && notes.map(renderItem)}
    </>
  );
}

function TicketNewDivider({ dividerRef }: { dividerRef: Ref<HTMLLIElement> }) {
  return (
    <li
      ref={dividerRef}
      data-testid="ticket-new-divider"
      className="flex scroll-mt-4 items-center gap-2 pb-4 text-xs font-semibold text-primary"
    >
      <span className="h-px flex-1 bg-primary/60" />
      New
      <span className="h-px flex-1 bg-primary/60" />
    </li>
  );
}

/** The completed ticket's plain-language result, pinned under the description and never folded. */
function TicketFinalUpdateSection({
  update,
  fallbackSessionId,
}: {
  update: TicketFinalUpdate;
  fallbackSessionId: string | null;
}) {
  return (
    <section
      aria-label="Final update"
      className="space-y-2 rounded-lg border border-border/60 bg-muted/30 px-3 py-2.5"
    >
      <h3 className="flex items-baseline justify-between gap-3 text-[length:calc(var(--text-ui)+2px)] font-bold">
        Final update
        <time
          className="shrink-0 text-sm font-normal text-muted-foreground tabular-nums"
          dateTime={new Date(update.created_at * 1000).toISOString()}
        >
          {absoluteTime(update.created_at * 1000)}
        </time>
      </h3>
      <TicketFileLinks sessionId={update.session_id ?? fallbackSessionId}>
        <TicketCommentBody body={update.text} />
      </TicketFileLinks>
    </section>
  );
}

function TicketActivityItem({
  event,
  personaLookup,
  nowMs,
  viewerId,
  fallbackSessionId,
  highlighted = false,
}: {
  event: TicketEvent;
  personaLookup: ReturnType<typeof useProjectPersonaLookup>;
  nowMs: number;
  viewerId: string | null;
  fallbackSessionId: string | null;
  /** The open question the viewer followed an Inbox link to. */
  highlighted?: boolean;
}) {
  const note = event.kind === "comment" || event.kind === "execution";
  const body = typeof event.payload.text === "string" ? event.payload.text : null;
  const outputImage =
    event.kind === "output" && event.payload.type === "artifact" ? ticketOutputs([event])[0] : null;
  const involvement =
    typeof event.payload.session_id === "string" && typeof event.payload.involvement === "string"
      ? event.payload.involvement
      : null;
  const agent = !!(event.session_id || event.agent_id);
  const author =
    event.user_id && event.user_id === viewerId
      ? "You"
      : (event.user_id ?? (agent ? "Agent" : "System"));
  const personaName = usePersonaForSession(event.session_id, personaLookup);
  const actor = event.session_id ? (
    <TicketSessionLink
      sessionId={event.session_id}
      showAgentName
      label={personaName ?? undefined}
    />
  ) : (
    <span className="text-foreground">{author}</span>
  );
  const stage = ticketEventStage(event);
  const age = relativeTime(event.created_at * 1000, nowMs);
  return (
    <li
      data-ticket-event-position={event.position}
      data-ask-highlight={highlighted || undefined}
      className={cn(
        "relative flex min-w-0 gap-3 pb-4 last:pb-0 before:absolute before:top-6 before:bottom-0 before:left-3 before:w-px before:bg-border/60 last:before:hidden",
        highlighted &&
          "-mx-2 rounded-md bg-status-yellow/10 px-2 pt-2 ring-1 ring-status-yellow/40",
      )}
    >
      <span
        className={cn(
          "relative flex size-6 shrink-0 items-center justify-center bg-background text-muted-foreground",
          note && "rounded-full bg-muted",
        )}
      >
        {note && personaName ? (
          <PersonaAvatar name={personaName} size="md" />
        ) : note ? (
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
          ) : event.payload.type === "artifact" ? (
            outputImage ? (
              <TicketArtifactEventIcon output={outputImage} />
            ) : (
              <FileIcon aria-hidden className="size-3.5" />
            )
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
              event.payload.final === true ? (
                <span className="ml-2">Final update</span>
              ) : (
                event.kind === "execution" && <span className="ml-2">Work update</span>
              )
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
        {involvement && (
          <p className="text-sm leading-relaxed whitespace-pre-wrap wrap-anywhere text-muted-foreground">
            {involvement}
          </p>
        )}
        {outputImage && <TicketArtifactImage output={outputImage} />}
        {body && (
          <TicketFileLinks sessionId={event.session_id ?? fallbackSessionId}>
            <TicketCommentBody body={body} mentions={payloadMentions(event.payload)} />
          </TicketFileLinks>
        )}
      </div>
    </li>
  );
}
