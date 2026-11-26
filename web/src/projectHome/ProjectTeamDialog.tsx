import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRightIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { Link } from "@/lib/routing";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import {
  createProjectIdentity,
  deleteProjectIdentity,
  PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES,
  updateProjectIdentity,
  type ProjectIdentity,
  type ProjectIdentityInput,
} from "@/lib/projectsApi";
import { IdentityModelEffortFields, type IdentitySessionChoice } from "./IdentityModelEffortFields";
import { ProjectKpiTiles } from "./ProjectKpis";
import { ProjectRoutingSection } from "./ProjectRoutingSection";
import { TicketError } from "./TicketControls";
import { useProjectRecord } from "./useProjectAgentSession";
import { projectIdentitiesKey, useProjectIdentities } from "./useProjectIdentities";

const encoder = new TextEncoder();
// Show the byte count once the instructions near their limit.
const BYTES_SHOWN_FROM = Math.floor(PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES * 0.8);

interface IdentityDraft extends IdentitySessionChoice {
  name: string;
  instructions: string;
  /** One scope item per line. */
  owns: string;
  excludes: string;
}

const EMPTY_DRAFT: IdentityDraft = {
  name: "",
  instructions: "",
  owns: "",
  excludes: "",
  agentId: "",
  model: "",
  effort: "",
};

/** Starting points for a new team member; each prefills its name, instructions and scope. */
export const ROLE_TEMPLATES: readonly Pick<
  IdentityDraft,
  "name" | "instructions" | "owns" | "excludes"
>[] = [
  {
    name: "Reviewer",
    instructions:
      "You are the code reviewer. Review each change for correctness, regressions and missing tests before it lands. Lead every comment with the exact commit you reviewed, separate blocking findings from suggestions, and approve only what you checked yourself.",
    owns: "Code review\nApproving commits",
    excludes: "Writing features\nProduct decisions",
  },
  {
    name: "Designer",
    instructions:
      "You are the product designer. Own how the product looks and feels: layouts, wording, states and accessibility. Turn requests into clear UI decisions, show before and after, and keep new screens consistent with the existing ones.",
    owns: "Visual design\nUX copy\nAccessibility",
    excludes: "Backend changes\nThe roadmap",
  },
  {
    name: "PM",
    instructions:
      "You are the product manager. Own scope, priorities and acceptance criteria. Turn requests into small, well-defined tickets with a clear done-when, keep the board ordered, and ask people for decisions only when they're really theirs to make. Write tickets, not code.",
    owns: "Priorities\nTicket scope\nAcceptance criteria",
    excludes: "Writing code\nCode review",
  },
  {
    name: "Engineer",
    instructions:
      "You are a software engineer. Take assigned tickets from plan to a tested, reviewable commit. Keep changes focused, run the relevant tests, and explain what changed and how you checked it when you hand off for review.",
    owns: "Implementation\nTests for your changes",
    excludes: "Approving your own work\nProduct decisions",
  },
];

const TEMPLATE_FIELDS = ["name", "instructions", "owns", "excludes"] as const;

/** Clears whatever a role template filled in, keeping the agent and model choice. */
const BLANK_TEMPLATE: (typeof ROLE_TEMPLATES)[number] = {
  name: "",
  instructions: "",
  owns: "",
  excludes: "",
};

function draftOf(identity: ProjectIdentity): IdentityDraft {
  return {
    name: identity.name,
    instructions: identity.instructions,
    owns: (identity.scope?.owns ?? []).join("\n"),
    excludes: (identity.scope?.excludes ?? []).join("\n"),
    agentId: identity.agent_id ?? "",
    model: identity.model ?? "",
    effort: identity.effort ?? "",
  };
}

function scopeLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function inputOf(draft: IdentityDraft, builtin: boolean): ProjectIdentityInput {
  const session = {
    instructions: draft.instructions,
    agent_id: draft.agentId.trim(),
    model: draft.model.trim(),
    effort: draft.effort.trim(),
  };
  // The Lead keeps its name and has no scope; its agent, model and effort are the
  // project's defaults.
  if (builtin) return session;
  return {
    name: draft.name.trim(),
    ...session,
    scope: { owns: scopeLines(draft.owns), excludes: scopeLines(draft.excludes) },
  };
}

/** Adds and edits the project's identities: named roles the user can talk to. */
export function ProjectTeamDialog({
  projectId,
  projectName,
  onClose,
}: {
  projectId: string;
  projectName: string;
  onClose: () => void;
}) {
  const identities = useProjectIdentities(projectId);
  const project = useProjectRecord(projectId).data;
  const shared = project?.shared === true;
  // `null` is the new-identity form; an id edits that identity.
  const [selected, setSelected] = useState<string | null>(null);
  // Routing replaces the member form while open; only the owner sees it.
  const [routingOpen, setRoutingOpen] = useState(false);
  const [draft, setDraft] = useState<IdentityDraft>(EMPTY_DRAFT);
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: projectIdentitiesKey(projectId) });
  const save = useMutation({
    mutationFn: (input: ProjectIdentityInput) =>
      selected === null
        ? createProjectIdentity(projectId, input)
        : updateProjectIdentity(projectId, selected, input),
    onSuccess: (identity) => {
      void refresh();
      setSelected(identity.id);
      setDraft(draftOf(identity));
    },
  });
  const remove = useMutation({
    mutationFn: (identityId: string) => deleteProjectIdentity(projectId, identityId),
    onSuccess: () => {
      void refresh();
      setSelected(null);
      setDraft(EMPTY_DRAFT);
    },
  });
  const all = identities.data ?? [];
  const roles = all.filter((identity) => !identity.builtin);
  const lead = all.find((identity) => identity.builtin);
  const selectedIdentity = all.find((identity) => identity.id === selected);
  const isLead = selectedIdentity?.builtin === true;
  // Only the owner edits the Lead's charter, as with the mandate; members still edit its model.
  const charterReadOnly = isLead && shared;
  const bytes = encoder.encode(draft.instructions.trim()).length;
  const tooLarge = bytes > PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES;
  const editing = selectedIdentity !== undefined;
  const busy = save.isPending || remove.isPending;
  const choose = (identity: ProjectIdentity | null) => {
    save.reset();
    remove.reset();
    setRoutingOpen(false);
    setSelected(identity?.id ?? null);
    setDraft(identity ? draftOf(identity) : EMPTY_DRAFT);
  };
  const canRoute = project !== undefined && !shared;
  const customRuntime = Boolean(draft.agentId || draft.model || draft.effort);
  // The Lead keeps its name, so only other roles show this field.
  const nameField = (
    <label className="flex flex-col gap-1 text-sm">
      Name
      <Input
        data-testid="project-team-name"
        value={draft.name}
        maxLength={60}
        placeholder="e.g. Designer"
        onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        disabled={busy}
      />
    </label>
  );
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.name.trim() && !tooLarge) save.mutate(inputOf(draft, isLead));
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl"
        data-testid="project-team-dialog"
      >
        <DialogHeader>
          <DialogTitle>Team</DialogTitle>
          <DialogDescription>
            Add specialists to <span className="font-medium">{projectName}</span> and assign them
            tickets from a ticket&apos;s Assignee picker.
          </DialogDescription>
        </DialogHeader>
        {identities.isLoading ? (
          <Spinner aria-label="Loading team" />
        ) : (
          <div className="flex flex-col gap-4 sm:flex-row">
            <nav className="flex shrink-0 flex-col gap-1 sm:w-44" aria-label="Team members">
              <Button
                type="button"
                variant={selected === null && !routingOpen ? "secondary" : "ghost"}
                size="sm"
                className="justify-start"
                data-testid="project-team-new"
                onClick={() => choose(null)}
              >
                <PlusIcon />
                New team member
              </Button>
              {lead && (
                <Button
                  type="button"
                  variant={selected === lead.id && !routingOpen ? "secondary" : "ghost"}
                  size="sm"
                  className="justify-start"
                  data-testid="project-team-lead"
                  onClick={() => choose(lead)}
                >
                  <PersonaAvatar name={lead.name} size="xs" />
                  <span className="truncate">{lead.name}</span>
                  <span className="ml-auto text-xs text-muted-foreground">Built-in</span>
                </Button>
              )}
              {roles.map((identity) => (
                <Button
                  key={identity.id}
                  type="button"
                  variant={selected === identity.id && !routingOpen ? "secondary" : "ghost"}
                  size="sm"
                  className="justify-start"
                  onClick={() => choose(identity)}
                >
                  <PersonaAvatar name={identity.name} size="xs" />
                  <span className="truncate">{identity.name}</span>
                </Button>
              ))}
              {canRoute && (
                <div className="mt-2 flex flex-col border-t border-border/60 pt-2">
                  <Button
                    type="button"
                    variant={routingOpen ? "secondary" : "ghost"}
                    size="sm"
                    className="justify-start text-muted-foreground"
                    data-testid="project-team-routing"
                    aria-pressed={routingOpen}
                    onClick={() => setRoutingOpen(true)}
                  >
                    Routing settings
                    <ChevronRightIcon className="ml-auto" />
                  </Button>
                </div>
              )}
            </nav>
            <div className="flex min-w-0 flex-1 flex-col">
              {routingOpen && canRoute ? (
                <section
                  className="flex flex-col gap-1"
                  aria-labelledby="project-team-routing-title"
                >
                  <h3 id="project-team-routing-title" className="text-sm font-medium">
                    Routing
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    Who receives ticket events, digests and escalations, and whether the project
                    keeps its built-in Lead.
                  </p>
                  <ProjectRoutingSection
                    projectId={projectId}
                    personas={roles}
                    className="mt-2 border-t-0 pt-0"
                  />
                </section>
              ) : (
                <>
                  <form onSubmit={onSubmit} className="flex min-w-0 flex-col gap-3">
                    {selected === null && (
                      <div
                        role="group"
                        aria-labelledby="project-team-templates-label"
                        className="flex flex-col gap-1.5 text-sm"
                      >
                        <span id="project-team-templates-label">
                          Start from a role{" "}
                          <span className="text-muted-foreground">(optional)</span>
                        </span>
                        <div className="flex flex-wrap gap-1.5">
                          {[BLANK_TEMPLATE, ...ROLE_TEMPLATES].map((template) => {
                            const pressed = TEMPLATE_FIELDS.every(
                              (field) => draft[field] === template[field],
                            );
                            return (
                              <Button
                                key={template.name || "blank"}
                                type="button"
                                variant="outline"
                                size="xs"
                                aria-pressed={pressed}
                                className="aria-pressed:border-brand-accent/40 aria-pressed:bg-brand-accent/10 aria-pressed:text-brand-accent dark:aria-pressed:border-brand-accent/40 dark:aria-pressed:bg-brand-accent/15"
                                disabled={busy}
                                onClick={() => setDraft({ ...draft, ...template })}
                              >
                                {template.name || "Blank"}
                              </Button>
                            );
                          })}
                        </div>
                      </div>
                    )}
                    {selectedIdentity && (
                      <div
                        className="flex items-center gap-2.5"
                        data-testid="project-team-identity"
                      >
                        <PersonaAvatar name={selectedIdentity.name} size="md" />
                        <div className="flex min-w-0 flex-col">
                          <h3 className="truncate text-base font-semibold">
                            {selectedIdentity.name}
                          </h3>
                          <span className="text-xs text-muted-foreground">
                            {isLead
                              ? "Built-in coordinator · can't be renamed"
                              : "Team member"}
                          </span>
                        </div>
                      </div>
                    )}
                    {!editing && nameField}
                    <label className="flex flex-col gap-1 text-sm">
                      <span className="flex flex-col">
                        <span className={editing ? "font-medium" : undefined}>
                          {isLead ? "Charter" : "Instructions"}
                        </span>
                        {editing && (
                          <span className="text-xs font-normal text-muted-foreground">
                            {isLead
                              ? "How the Lead coordinates this project."
                              : `What ${selectedIdentity.name} is responsible for and how it works.`}
                          </span>
                        )}
                      </span>
                      <Textarea
                        data-testid="project-team-persona"
                        value={draft.instructions}
                        placeholder={
                          isLead
                            ? "e.g. Batch reviews into one daily summary. Prefer fewer, larger tickets."
                            : "e.g. You are the product manager. Own scope, priorities and acceptance criteria; write tickets, don't write code."
                        }
                        className="min-h-36 max-h-[45dvh] text-sm"
                        aria-invalid={tooLarge || undefined}
                        onChange={(event) =>
                          setDraft({ ...draft, instructions: event.target.value })
                        }
                        disabled={busy || charterReadOnly}
                      />
                      {bytes >= BYTES_SHOWN_FROM && (
                        <span
                          data-testid="project-team-bytes"
                          className={
                            tooLarge ? "text-xs text-destructive" : "text-xs text-muted-foreground"
                          }
                        >
                          {bytes.toLocaleString()} /{" "}
                          {PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES.toLocaleString()} bytes
                          {tooLarge ? ". Shorten it to save." : ""}
                        </span>
                      )}
                      {editing && (
                        <span
                          className="text-xs text-muted-foreground"
                          data-testid={isLead ? "project-team-lead-note" : "project-team-timing"}
                        >
                          {isLead
                            ? `The project mandate also applies. Charter changes apply on the Lead's next turn or session start; agent and model changes from its next session or rollover. ${
                                charterReadOnly
                                  ? "Only the project owner edits the charter."
                                  : "Only you can edit the charter; agents can't."
                              }`
                            : `The project mandate also applies. Changes apply from ${selectedIdentity.name}'s next session.`}
                        </span>
                      )}
                    </label>
                    {!isLead && (
                      <div className="grid gap-3 sm:grid-cols-2">
                        <label className="flex flex-col gap-1 text-sm">
                          Owns
                          <Textarea
                            data-testid="project-team-owns"
                            value={draft.owns}
                            placeholder={"One per line, e.g.\nVisual design\nUX reviews"}
                            className="min-h-20 text-sm"
                            onChange={(event) => setDraft({ ...draft, owns: event.target.value })}
                            disabled={busy}
                          />
                        </label>
                        <label className="flex flex-col gap-1 text-sm">
                          Doesn&apos;t own
                          <Textarea
                            data-testid="project-team-excludes"
                            value={draft.excludes}
                            placeholder={"One per line, e.g.\nThe roadmap\nCode review"}
                            className="min-h-20 text-sm"
                            onChange={(event) =>
                              setDraft({ ...draft, excludes: event.target.value })
                            }
                            disabled={busy}
                          />
                        </label>
                      </div>
                    )}
                    {editing && !isLead && nameField}
                    {/* Keyed by member so each starts collapsed. */}
                    <Collapsible
                      key={selected ?? "new"}
                      className="rounded-lg border border-border/60"
                    >
                      <CollapsibleTrigger
                        data-testid="project-team-runtime-toggle"
                        className="group flex w-full cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-left text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                      >
                        <span>
                          Agent &amp; model{" "}
                          <span className="text-muted-foreground">
                            · {customRuntime ? "Custom" : "Project defaults"}
                          </span>
                        </span>
                        <ChevronRightIcon className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform duration-200 ease-out group-data-[state=open]:rotate-90" />
                      </CollapsibleTrigger>
                      <CollapsibleContent className="flex flex-col gap-2 px-3 pb-3">
                        {isLead && (
                          <span className="text-xs text-muted-foreground">
                            The Lead&apos;s agent, model and effort are also the project&apos;s
                            defaults for new sessions.
                          </span>
                        )}
                        <IdentityModelEffortFields
                          config={project?.config}
                          agentId={draft.agentId}
                          model={draft.model}
                          effort={draft.effort}
                          disabled={busy}
                          agentLocked={shared}
                          defaultLabel={isLead ? "Default" : "Project default"}
                          onChange={(next) => setDraft({ ...draft, ...next })}
                        />
                      </CollapsibleContent>
                    </Collapsible>
                    {!isLead && (
                      <p
                        className="text-xs text-muted-foreground"
                        data-testid="project-team-own-copy"
                      >
                        This team member gets its own copy, so you can change its tools without
                        touching the original.
                      </p>
                    )}
                    {!isLead && !shared && selectedIdentity?.project_agent_id && (
                      <div
                        className="flex flex-col gap-1 rounded-md border border-border/60 p-3 text-sm"
                        data-testid="project-team-tools"
                      >
                        <span className="font-medium">Tools and permissions</span>
                        <span className="text-xs text-muted-foreground">
                          Give this team member its own MCP servers or permission mode. Only you can
                          change them; they apply from its next session.
                        </span>
                        <Link
                          className="text-xs underline underline-offset-2"
                          to={`/settings/custom-agents/${encodeURIComponent(selectedIdentity.project_agent_id)}`}
                        >
                          Edit {selectedIdentity.name}&apos;s tools and permissions
                        </Link>
                      </div>
                    )}
                    {shared && (
                      <p
                        className="text-xs text-muted-foreground"
                        data-testid="project-team-agent-owner"
                      >
                        Only the project owner chooses the agent, since these sessions run as them.
                      </p>
                    )}
                    {selectedIdentity?.created_by_session_id && (
                      <p
                        className="text-xs text-muted-foreground"
                        data-testid="project-team-agent-made"
                      >
                        Created by an agent, so agents can also edit it. Team members you create
                        stay yours to edit.
                      </p>
                    )}
                    <TicketError error={save.error ?? remove.error ?? identities.error} />
                    {selected === null && (
                      <p className="text-xs text-muted-foreground">
                        All team members follow the project mandate.
                      </p>
                    )}
                    {/* Sticky so Add stays reachable on phones while the form scrolls. */}
                    <DialogFooter className="sticky -bottom-6 z-10 mb-0 border-t-0 bg-white pt-3 pb-6 sm:mx-0 sm:px-0 dark:bg-(--menu-surface-bg)">
                      {selected !== null && !isLead && (
                        <Button
                          type="button"
                          variant="ghost"
                          className="mr-auto text-destructive"
                          loading={remove.isPending}
                          disabled={busy}
                          onClick={() => remove.mutate(selected)}
                        >
                          <Trash2Icon />
                          Remove
                        </Button>
                      )}
                      <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
                        Close
                      </Button>
                      <Button
                        type="submit"
                        data-testid="project-team-save"
                        loading={save.isPending}
                        disabled={!draft.name.trim() || tooLarge || remove.isPending}
                      >
                        {selected === null ? "Add member" : "Save"}
                      </Button>
                    </DialogFooter>
                  </form>
                  {selectedIdentity && !isLead && (
                    <div className="mt-4 border-t border-border/60 pt-4">
                      <ProjectKpiTiles
                        projectId={projectId}
                        identityId={selectedIdentity.id}
                        title={`${selectedIdentity.name} KPIs`}
                      />
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
