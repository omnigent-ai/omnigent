import { useEffect, useState, type ComponentProps, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { getProject, PROJECT_MANDATE_MAX_BYTES, updateProjectMandate } from "@/lib/projectsApi";
import { TicketError } from "./TicketControls";
import { useTicketProject } from "./useProjectTickets";

const encoder = new TextEncoder();

/** Bytes the server counts against the limit: it stores the trimmed text. */
function mandateBytes(text: string): number {
  return encoder.encode(text.trim()).length;
}

/** Edits the guidance every agent session in the project receives as its bounds. */
export function ProjectMandateDialog({
  projectName,
  onClose,
  onCloseAutoFocus,
}: {
  projectName: string;
  onClose: () => void;
  onCloseAutoFocus?: ComponentProps<typeof DialogContent>["onCloseAutoFocus"];
}) {
  const { projectId, projects, ensure } = useTicketProject(projectName);
  const query = useQuery({
    queryKey: ["project-mandate", projectId],
    queryFn: async () => (await getProject(projectId as string)).mandate ?? "",
    enabled: projectId !== null,
  });
  // A label-only folder has no row yet; it starts blank and saving creates it.
  const [draft, setDraft] = useState<string | null>(null);
  useEffect(() => {
    if (draft !== null || projects.isLoading || projects.error) return;
    if (projectId === null) setDraft("");
    else if (query.data !== undefined) setDraft(query.data);
  }, [draft, projects.isLoading, projects.error, projectId, query.data]);
  const loadError = projects.error ?? query.error;
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: async (mandate: string) => {
      const id = projectId ?? (await ensure.mutateAsync());
      return updateProjectMandate(id, mandate);
    },
    onSuccess: (project) => {
      client.setQueryData(["project-mandate", project.id], project.mandate ?? "");
      onClose();
    },
  });
  const bytes = draft === null ? 0 : mandateBytes(draft);
  const tooLarge = bytes > PROJECT_MANDATE_MAX_BYTES;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (draft !== null && !tooLarge) save.mutate(draft);
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) onClose();
      }}
    >
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl"
        onCloseAutoFocus={onCloseAutoFocus}
      >
        <DialogHeader>
          <DialogTitle>Mandate</DialogTitle>
          <DialogDescription>
            Bounds every agent in <span className="font-medium">{projectName}</span> follows,
            including the Orchestrator and its workers. Running sessions may keep the previous
            mandate until they restart.
          </DialogDescription>
        </DialogHeader>
        {draft !== null ? (
          <form onSubmit={onSubmit} className="flex flex-col gap-2">
            <Textarea
              aria-label="Project mandate"
              data-testid="project-mandate-input"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="e.g. Only change files under docs/. Never push or open pull requests."
              aria-invalid={tooLarge || undefined}
              className="min-h-48 max-h-[55dvh] font-mono text-sm"
              disabled={save.isPending}
            />
            <p
              className={tooLarge ? "text-sm text-destructive" : "text-sm text-muted-foreground"}
              data-testid="project-mandate-size"
            >
              {bytes.toLocaleString()} / {PROJECT_MANDATE_MAX_BYTES.toLocaleString()} bytes
            </p>
            <TicketError error={save.error} />
            <DialogFooter className="border-t-0 bg-transparent">
              <Button type="button" variant="ghost" onClick={onClose} disabled={save.isPending}>
                Cancel
              </Button>
              <Button
                type="submit"
                data-testid="project-mandate-save"
                loading={save.isPending}
                disabled={tooLarge}
              >
                Save
              </Button>
            </DialogFooter>
          </form>
        ) : (
          <>
            {!loadError && <Spinner aria-label="Loading mandate" />}
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
