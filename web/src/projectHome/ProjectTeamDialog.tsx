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
import { ProjectRoutingSection } from "./ProjectRoutingSection";
import { TicketError } from "./TicketControls";
import { projectIdentitiesKey, useProjectIdentities } from "./useProjectIdentities";

const encoder = new TextEncoder();

interface IdentityDraft {
  name: string;
  instructions: string;
  model: string;
  effort: string;
}

const EMPTY_DRAFT: IdentityDraft = { name: "", instructions: "", model: "", effort: "" };

function draftOf(identity: ProjectIdentity): IdentityDraft {
  return {
    name: identity.name,
    instructions: identity.instructions,
    model: identity.model ?? "",
    effort: identity.effort ?? "",
  };
}

function inputOf(draft: IdentityDraft): ProjectIdentityInput {
  return {
    name: draft.name.trim(),
    instructions: draft.instructions,
    model: draft.model.trim(),
    effort: draft.effort.trim(),
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
  const roles = (identities.data ?? []).filter((identity) => !identity.builtin);
  const selectedIdentity = roles.find((identity) => identity.id === selected);
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
    if (draft.name.trim() && !tooLarge) save.mutate(inputOf(draft));
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
            beside the Lead. Agents can add roles too. Each works in one session at a time and
            follows the project mandate. Assign a ticket with the label{" "}
            <span className="font-mono">assignee: Name</span>.
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
                    disabled={busy}
                  />
                </label>
                <label className="flex flex-col gap-1 text-sm">
                  Persona
                  <Textarea
                    data-testid="project-team-persona"
                    value={draft.instructions}
                    placeholder="e.g. You are the product manager. Own scope, priorities and acceptance criteria; write tickets, don't write code."
                    className="min-h-36 max-h-[45dvh] text-sm"
                    aria-invalid={tooLarge || undefined}
                    onChange={(event) => setDraft({ ...draft, instructions: event.target.value })}
                    disabled={busy}
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
                <div className="flex gap-3">
                  <label className="flex flex-1 flex-col gap-1 text-sm">
                    Model
                    <Input
                      value={draft.model}
                      placeholder="Project default"
                      onChange={(event) => setDraft({ ...draft, model: event.target.value })}
                      disabled={busy}
                    />
                  </label>
                  <label className="flex flex-1 flex-col gap-1 text-sm">
                    Effort
                    <Input
                      value={draft.effort}
                      placeholder="Project default"
                      onChange={(event) => setDraft({ ...draft, effort: event.target.value })}
                      disabled={busy}
                    />
                  </label>
                </div>
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
                  {selected !== null && (
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
