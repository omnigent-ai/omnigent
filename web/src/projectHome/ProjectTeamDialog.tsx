import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { Button } from "@/components/ui/button";
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
import { ProjectRoutingSection } from "./ProjectRoutingSection";
import { TicketError } from "./TicketControls";
import { useProjectRecord } from "./useProjectAgentSession";
import { projectIdentitiesKey, useProjectIdentities } from "./useProjectIdentities";

const encoder = new TextEncoder();

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
  const busy = save.isPending || remove.isPending;
  const choose = (identity: ProjectIdentity | null) => {
    save.reset();
    remove.reset();
    setSelected(identity?.id ?? null);
    setDraft(identity ? draftOf(identity) : EMPTY_DRAFT);
  };
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
            Persistent roles in <span className="font-medium">{projectName}</span> you can talk to,
            including the built-in Lead, whose charter you can edit. Agents can add roles too. Each
            works in one session at a time and follows the project mandate. Assign a ticket with the
            label <span className="font-mono">assignee: Name</span>.
          </DialogDescription>
        </DialogHeader>
        {identities.isLoading ? (
          <Spinner aria-label="Loading team" />
        ) : (
          <>
            <div className="flex flex-col gap-4 sm:flex-row">
              <nav className="flex shrink-0 flex-col gap-1 sm:w-44" aria-label="Identities">
                <Button
                  type="button"
                  variant={selected === null ? "secondary" : "ghost"}
                  size="sm"
                  className="justify-start"
                  data-testid="project-team-new"
                  onClick={() => choose(null)}
                >
                  <PlusIcon />
                  New identity
                </Button>
                {lead && (
                  <Button
                    type="button"
                    variant={selected === lead.id ? "secondary" : "ghost"}
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
                    variant={selected === identity.id ? "secondary" : "ghost"}
                    size="sm"
                    className="justify-start"
                    onClick={() => choose(identity)}
                  >
                    <PersonaAvatar name={identity.name} size="xs" />
                    <span className="truncate">{identity.name}</span>
                  </Button>
                ))}
              </nav>
              <form onSubmit={onSubmit} className="flex min-w-0 flex-1 flex-col gap-3">
                <label className="flex flex-col gap-1 text-sm">
                  Name
                  <Input
                    data-testid="project-team-name"
                    value={draft.name}
                    maxLength={60}
                    placeholder="e.g. PM, Designer"
                    onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                    disabled={busy || isLead}
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm">
                  {isLead ? "Charter" : "Persona"}
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
                    onChange={(event) => setDraft({ ...draft, instructions: event.target.value })}
                    disabled={busy || charterReadOnly}
                  />
                  <span
                    className={
                      tooLarge ? "text-xs text-destructive" : "text-xs text-muted-foreground"
                    }
                  >
                    {bytes.toLocaleString()} /{" "}
                    {PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES.toLocaleString()} bytes
                  </span>
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
                        onChange={(event) => setDraft({ ...draft, excludes: event.target.value })}
                        disabled={busy}
                      />
                    </label>
                  </div>
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
                {shared && (
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid="project-team-agent-owner"
                  >
                    Only the project owner chooses the agent, since these sessions run as them.
                  </p>
                )}
                {isLead && (
                  <p className="text-xs text-muted-foreground" data-testid="project-team-lead-note">
                    The Lead is the project's built-in coordinator and can't be renamed or deleted.
                    {charterReadOnly
                      ? " Only the project owner edits its charter."
                      : " Only you can edit its charter; agents can't."}{" "}
                    The charter applies on its next turn or session start. Its agent, model and
                    effort are also the project's defaults for new sessions, and they apply from the
                    Lead's next session or rollover.
                  </p>
                )}
                {selectedIdentity?.created_by_session_id && (
                  <p
                    className="text-xs text-muted-foreground"
                    data-testid="project-team-agent-made"
                  >
                    Created by an agent, so agents can also edit it. Identities you create stay
                    yours to edit.
                  </p>
                )}
                <TicketError error={save.error ?? remove.error ?? identities.error} />
                <DialogFooter className="border-t-0 bg-transparent">
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
                    {selected === null ? "Add" : "Save"}
                  </Button>
                </DialogFooter>
              </form>
            </div>
            <ProjectRoutingSection projectId={projectId} personas={roles} />
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
