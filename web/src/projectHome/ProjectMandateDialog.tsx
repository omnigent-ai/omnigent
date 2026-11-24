import { useEffect, useState, type ComponentProps, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FilePathAwareMessageResponse, ProseMarkdown } from "@/components/blocks/ChatMarkdown";
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
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { textLineDiff } from "@/lib/toolEditDiff";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import {
  acceptMandateProposal,
  dismissMandateProposal,
  getProject,
  PROJECT_MANDATE_LARGE_BYTES,
  PROJECT_MANDATE_MAX_BYTES,
  updateProjectMandate,
  type MandateProposal,
  type Project,
} from "@/lib/projectsApi";
import { TicketError } from "./TicketControls";
import { useTicketProject } from "./useProjectTickets";

const encoder = new TextEncoder();

// Document-scale headings for a long mandate: distinct from body text, but sized for a dialog.
const MANDATE_PREVIEW_CLASS =
  "text-sm [&_[data-streamdown=heading-1]]:text-lg [&_[data-streamdown=heading-2]]:text-base [&_[data-streamdown=heading-3]]:text-sm [&_[data-streamdown^=heading]]:mt-5 [&_[data-streamdown^=heading]]:mb-1.5";

/** Bytes the server counts against the limit: it stores the trimmed text. */
function mandateBytes(text: string): number {
  return encoder.encode(text.trim()).length;
}

/** What a proposal does, in the owner's terms. */
function proposalLabel(proposal: MandateProposal): string | null {
  if (proposal.mode === "replace_all") return "Replace the whole mandate";
  if (proposal.mode === "replace_section") {
    return `${proposal.text.trim() ? "Replace" : "Delete"} section “${proposal.section ?? ""}”`;
  }
  return null;
}

/** Removed and added lines of a replacement proposal against the current mandate. */
function ProposalDiff({ proposal }: { proposal: MandateProposal }) {
  // Kind and text plus a running repeat count keep keys unique and stable.
  const seen = new Map<string, number>();
  const lines = textLineDiff(proposal.current_text ?? "", proposal.text).map((line) => {
    const base = `${line.kind}:${line.text}`;
    const repeat = seen.get(base) ?? 0;
    seen.set(base, repeat + 1);
    return { ...line, key: `${base}#${repeat}` };
  });
  return (
    <pre
      className="max-h-64 overflow-auto rounded-md border border-border bg-muted/40 py-1 font-mono text-xs leading-relaxed"
      data-testid="project-mandate-proposal-diff"
    >
      {lines.map((line) => (
        <div
          key={line.key}
          data-kind={line.kind}
          className={cn(
            "whitespace-pre-wrap px-2",
            line.kind === "added" && "bg-emerald-500/15 text-emerald-800 dark:text-emerald-300",
            line.kind === "removed" &&
              "bg-red-500/15 text-red-800 line-through decoration-red-500/40 dark:text-red-300",
            line.kind === "gap" && "text-muted-foreground",
          )}
        >
          {line.kind === "gap"
            ? "⋯"
            : `${line.kind === "added" ? "+" : line.kind === "removed" ? "−" : " "} ${line.text}`}
        </div>
      ))}
    </pre>
  );
}

/** The project's mandate and pending proposals; agents' proposals refresh it live. */
export function useProjectMandate(projectId: string | null, enabled = true) {
  return useQuery<Project>({
    queryKey: ["project-mandate", projectId],
    queryFn: () => getProject(projectId as string),
    enabled: projectId !== null && enabled,
    staleTime: 30_000,
  });
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
  const query = useProjectMandate(projectId);
  // The text the draft started from, to tell unsaved edits apart from server changes.
  const [loaded, setLoaded] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  // An existing mandate opens rendered for reading; a blank one opens ready to write.
  const [mode, setMode] = useState<"preview" | "edit">("preview");
  useEffect(() => {
    if (draft !== null || projects.isLoading || projects.error) return;
    // A label-only folder has no row yet; it starts blank and saving creates it.
    const initial = projectId === null ? "" : query.data ? (query.data.mandate ?? "") : null;
    if (initial === null) return;
    setLoaded(initial);
    setDraft(initial);
    if (!initial.trim()) setMode("edit");
  }, [draft, projects.isLoading, projects.error, projectId, query.data]);
  const loadError = projects.error ?? query.error;
  const client = useQueryClient();
  const remember = (project: Project) =>
    client.setQueryData(["project-mandate", project.id], project);
  const save = useMutation({
    mutationFn: async (mandate: string) => {
      const id = projectId ?? (await ensure.mutateAsync());
      return updateProjectMandate(id, mandate);
    },
    onSuccess: (project) => {
      remember(project);
      onClose();
    },
  });
  const resolve = useMutation({
    mutationFn: ({ proposal, accept }: { proposal: MandateProposal; accept: boolean }) =>
      (accept ? acceptMandateProposal : dismissMandateProposal)(projectId as string, proposal.id),
    onSuccess: (project, { proposal, accept }) => {
      remember(project);
      if (!accept || draft === null) return;
      const accepted = project.mandate ?? "";
      if ((proposal.mode ?? "append") !== "append") {
        // Replacements are only accepted without unsaved edits, so the draft follows the server.
        setLoaded(accepted);
        setDraft(accepted);
        return;
      }
      // Keep unsaved edits, adding the accepted text so saving them doesn't drop it.
      const next =
        draft === loaded ? accepted : [draft.trim(), proposal.text].filter(Boolean).join("\n\n");
      setLoaded(accepted);
      setDraft(next);
    },
  });
  const proposals = query.data?.mandate_proposals ?? [];
  const busy = save.isPending || resolve.isPending;
  const bytes = draft === null ? 0 : mandateBytes(draft);
  const tooLarge = bytes > PROJECT_MANDATE_MAX_BYTES;
  const large = !tooLarge && bytes > PROJECT_MANDATE_LARGE_BYTES;
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (draft !== null && !tooLarge) save.mutate(draft);
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
          <DialogTitle>Mandate</DialogTitle>
          <DialogDescription>
            Bounds every agent in <span className="font-medium">{projectName}</span> follows,
            including the Lead and its workers. It overrides conflicting repo instructions such as
            CLAUDE.md, but never safety rules or approval prompts. Running sessions may keep the
            previous mandate until they restart.
          </DialogDescription>
        </DialogHeader>
        {draft !== null ? (
          <form onSubmit={onSubmit} className="flex flex-col gap-2">
            {proposals.length > 0 && (
              <section
                aria-label="Proposed changes"
                className="flex flex-col gap-2"
                data-testid="project-mandate-proposals"
              >
                <h3 className="font-medium text-ui">Proposed changes</h3>
                <p className="text-sm text-muted-foreground">
                  Agents suggested these from your feedback. They apply only once you accept them.
                </p>
                {proposals.map((proposal) => {
                  const label = proposalLabel(proposal);
                  const blockedByEdits = label !== null && draft !== loaded;
                  return (
                    <div
                      key={proposal.id}
                      className="flex flex-col gap-2 rounded-lg border border-border p-2.5 sm:flex-row sm:items-start"
                      data-testid="project-mandate-proposal"
                      data-stale={proposal.stale || undefined}
                    >
                      {label === null ? (
                        <ProseMarkdown className="min-w-0 flex-1 text-sm">
                          {proposal.text}
                        </ProseMarkdown>
                      ) : (
                        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                          <p className="text-sm font-medium">{label}</p>
                          {proposal.stale && (
                            <p
                              className="text-sm text-amber-700 dark:text-amber-400"
                              data-testid="project-mandate-proposal-stale"
                            >
                              Out of date: the mandate changed since this was proposed. Dismiss it
                              and ask for a new one.
                            </p>
                          )}
                          {blockedByEdits && !proposal.stale && (
                            <p className="text-sm text-muted-foreground">
                              Save or cancel your edits before accepting a replacement.
                            </p>
                          )}
                          <ProposalDiff proposal={proposal} />
                        </div>
                      )}
                      <div className="flex shrink-0 gap-1">
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => resolve.mutate({ proposal, accept: false })}
                        >
                          Dismiss
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={busy || proposal.stale || blockedByEdits}
                          onClick={() => resolve.mutate({ proposal, accept: true })}
                        >
                          Accept
                        </Button>
                      </div>
                    </div>
                  );
                })}
                <TicketError error={resolve.error} />
              </section>
            )}
            <Tabs value={mode} onValueChange={(value) => setMode(value as "preview" | "edit")}>
              <TabsList aria-label="Mandate view">
                <TabsTrigger value="preview" data-testid="project-mandate-preview-tab">
                  Preview
                </TabsTrigger>
                <TabsTrigger value="edit" data-testid="project-mandate-edit-tab">
                  Edit
                </TabsTrigger>
              </TabsList>
            </Tabs>
            {mode === "edit" ? (
              <Textarea
                aria-label="Project mandate"
                data-testid="project-mandate-input"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="e.g. Only change files under docs/. Never push or open pull requests."
                aria-invalid={tooLarge || undefined}
                className="min-h-48 max-h-[55dvh] font-mono text-sm"
                disabled={busy}
              />
            ) : (
              <div
                role="region"
                aria-label="Mandate preview"
                data-testid="project-mandate-preview"
                className="min-h-48 max-h-[55dvh] overflow-y-auto rounded-md border border-border px-4 py-3"
              >
                {draft.trim() ? (
                  <FilePathAwareMessageResponse
                    mode="static"
                    literalHtml
                    className={MANDATE_PREVIEW_CLASS}
                  >
                    {draft}
                  </FilePathAwareMessageResponse>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    No mandate yet. Choose Edit to write one.
                  </p>
                )}
              </div>
            )}
            <p
              className={tooLarge ? "text-sm text-destructive" : "text-sm text-muted-foreground"}
              data-testid="project-mandate-size"
            >
              {bytes.toLocaleString()} / {PROJECT_MANDATE_MAX_BYTES.toLocaleString()} bytes
              {large &&
                ` · Every session in this project loads the mandate, about ${Math.round(bytes / 4).toLocaleString()} tokens each. Consider trimming it.`}
            </p>
            <TicketError error={save.error} />
            <DialogFooter className="border-t-0 bg-transparent">
              <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
                Cancel
              </Button>
              <Button
                type="submit"
                data-testid="project-mandate-save"
                loading={save.isPending}
                disabled={tooLarge || resolve.isPending}
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
