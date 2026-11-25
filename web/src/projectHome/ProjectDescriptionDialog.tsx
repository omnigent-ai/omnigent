import { useEffect, useState, type ComponentProps, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import {
  PROJECT_DESCRIPTION_MAX_BYTES,
  updateProjectDescription,
  type Project,
} from "@/lib/projectsApi";
import { useProjectMandate } from "./ProjectMandateDialog";
import { TicketError } from "./TicketControls";
import { useTicketProject } from "./useProjectTickets";

const encoder = new TextEncoder();

/** The sections a charter covers, shown as the empty field's placeholder. */
export const PROJECT_DESCRIPTION_PLACEHOLDER = [
  "Mission: what this project is for.",
  "Scope: what it covers.",
  "Out of scope: what it won't do.",
  "How we'll know it's working: the signals or numbers to watch.",
  "Starting roster: the people and agent team members.",
].join("\n");

/** Bytes the server counts against the limit: it stores the trimmed text. */
export function descriptionBytes(text: string): number {
  return encoder.encode(text.trim()).length;
}

/**
 * Reads or edits the project's description: its charter for people, separate from
 * the mandate agents follow. The owner edits it (the Lead can, through its tool);
 * shared members see it read-only.
 */
export function ProjectDescriptionDialog({
  projectName,
  onClose,
  onCloseAutoFocus,
}: {
  projectName: string;
  onClose: () => void;
  onCloseAutoFocus?: ComponentProps<typeof DialogContent>["onCloseAutoFocus"];
}) {
  const { projectId, projects, ensure } = useTicketProject(projectName);
  // The project query the mandate dialog uses; both read the same project row.
  const query = useProjectMandate(projectId);
  const [draft, setDraft] = useState<string | null>(null);
  useEffect(() => {
    if (draft !== null || projects.isLoading || projects.error) return;
    const initial = projectId === null ? "" : query.data ? (query.data.description ?? "") : null;
    if (initial === null) return;
    setDraft(initial);
  }, [draft, projects.isLoading, projects.error, projectId, query.data]);
  const readOnly = query.data?.shared === true;
  const loadError = projects.error ?? query.error;
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: async (description: string) => {
      const id = projectId ?? (await ensure.mutateAsync());
      return updateProjectDescription(id, description);
    },
    onSuccess: (project: Project) => {
      client.setQueryData(["project-mandate", project.id], project);
      void client.invalidateQueries({ queryKey: ["project-status-summary", project.id] });
      onClose();
    },
  });
  const busy = save.isPending;
  const bytes = draft === null ? 0 : descriptionBytes(draft);
  const tooLarge = bytes > PROJECT_DESCRIPTION_MAX_BYTES;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (draft !== null && !tooLarge && !readOnly) save.mutate(draft);
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
        onCloseAutoFocus={onCloseAutoFocus}
      >
        <DialogHeader>
          <DialogTitle>Project description</DialogTitle>
          <DialogDescription>
            The charter for people reading <span className="font-medium">{projectName}</span>: its
            mission, scope, what&rsquo;s out of scope, how you&rsquo;ll know it&rsquo;s working and
            the starting roster. Agents don&rsquo;t follow it; that&rsquo;s the mandate.{" "}
            {readOnly
              ? "Only the project owner or its Lead can edit it."
              : "Only you and the project's Lead can edit it."}
          </DialogDescription>
        </DialogHeader>
        {draft !== null ? (
          <form onSubmit={onSubmit} className="flex flex-col gap-2">
            {readOnly && draft.trim() === "" ? (
              <p className="text-sm text-muted-foreground" data-testid="project-description-empty">
                No description yet.
              </p>
            ) : (
              <Textarea
                aria-label="Project description"
                data-testid="project-description-input"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={PROJECT_DESCRIPTION_PLACEHOLDER}
                aria-invalid={tooLarge || undefined}
                className="min-h-48 max-h-[55dvh] text-sm"
                readOnly={readOnly}
                disabled={busy}
              />
            )}
            {!readOnly && (
              <p
                className={tooLarge ? "text-sm text-destructive" : "text-sm text-muted-foreground"}
                data-testid="project-description-size"
              >
                {bytes.toLocaleString()} / {PROJECT_DESCRIPTION_MAX_BYTES.toLocaleString()} bytes
              </p>
            )}
            <TicketError error={save.error} />
            <DialogFooter className="border-t-0 bg-transparent">
              <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
                {readOnly ? "Close" : "Cancel"}
              </Button>
              {!readOnly && (
                <Button
                  type="submit"
                  data-testid="project-description-save"
                  loading={save.isPending}
                  disabled={tooLarge}
                >
                  Save
                </Button>
              )}
            </DialogFooter>
          </form>
        ) : (
          <>
            {!loadError && <Spinner aria-label="Loading description" />}
            <TicketError error={loadError} />
            {loadError && (
              <Button
                variant="ghost"
                onClick={() => void (projects.error ? projects.refetch() : query.refetch())}
              >
                Try again
              </Button>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
