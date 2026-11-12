/**
 * Workflows page (`/workflows`): the user's saved workflows, a search, and a
 * "New workflow" button. Laid out like the Automations page.
 *
 * New workflow opens the new-session page with a request for the agent to draft
 * and save the workflow. Delete removes one; Run, Edit and Create automation
 * aren't connected yet.
 */

import { useMemo, useState } from "react";
import { Loader2Icon, SearchIcon, TriangleAlertIcon, WorkflowIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { NewWorkflowDialog } from "@/components/workflows/NewWorkflowDialog";
import { WorkflowRow } from "@/components/workflows/WorkflowRow";
import { useDeleteWorkflow, useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import { useNavigate } from "@/lib/routing";
import { createWorkflowPrompt } from "@/lib/workflow";

export function WorkflowsPage() {
  const { workflows, isLoading, isError, refresh } = useSavedWorkflows();
  const deleteWorkflow = useDeleteWorkflow();
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);

  const hasAnyWorkflows = workflows.length > 0;
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) return workflows;
    return workflows.filter(
      (workflow) =>
        workflow.name.toLowerCase().includes(query) ||
        workflow.description.toLowerCase().includes(query),
    );
  }, [workflows, search]);
  const takenNames = useMemo(() => workflows.map((workflow) => workflow.name), [workflows]);

  return (
    <PageScroll className="[scrollbar-gutter:stable]" contentClassName="px-4 md:px-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Workflows</h1>
          <p className="text-ui text-muted-foreground">
            Reusable step-by-step routines that agents can run for you.
          </p>
        </div>
        <Button
          data-testid="new-workflow-button"
          className="shrink-0"
          componentId="workflows.new"
          onClick={() => setCreating(true)}
        >
          New workflow
        </Button>
      </div>

      {hasAnyWorkflows && (
        <div className="relative mb-4">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search workflows…"
            componentId="workflows.search"
            data-testid="workflows-search"
            className="pl-9"
          />
        </div>
      )}

      {isError ? (
        <div
          role="alert"
          data-testid="workflows-load-error"
          className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui"
        >
          <TriangleAlertIcon className="size-4 shrink-0 text-destructive" />
          <span className="flex-1">Couldn’t load workflows.</span>
          <Button variant="outline" size="sm" onClick={refresh} componentId="workflows.retry">
            Retry
          </Button>
        </div>
      ) : isLoading ? (
        <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading workflows…
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState hasAny={hasAnyWorkflows} />
      ) : (
        <div className="flex flex-col gap-2" data-testid="workflows-list">
          {filtered.map((workflow) => (
            <WorkflowRow
              key={workflow.id}
              workflow={workflow}
              onRun={() => showToast("Running workflows isn’t connected yet.")}
              onEdit={() => showToast("Editing workflows isn’t connected yet.")}
              onCreateAutomation={() =>
                showToast("Creating automations from workflows isn’t connected yet.")
              }
              onDelete={() =>
                deleteWorkflow.mutate(workflow.id, {
                  onError: () => showToast(`Couldn’t delete “${workflow.name}”.`),
                })
              }
            />
          ))}
        </div>
      )}

      <NewWorkflowDialog
        open={creating}
        onOpenChange={setCreating}
        onCreate={({ name, description }) =>
          navigate(`/?prompt=${encodeURIComponent(createWorkflowPrompt(name, description))}`)
        }
        takenNames={takenNames}
      />
    </PageScroll>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  return (
    <div className="py-8" data-testid="workflows-empty-state">
      {hasAny ? (
        <div className="py-10 text-center text-ui text-muted-foreground">No workflows found</div>
      ) : (
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <WorkflowIcon className="size-8 text-muted-foreground/50" />
          <p className="text-ui font-medium">No workflows yet</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            Create a workflow to run the same multi-step work whenever you need it.
          </p>
        </div>
      )}
    </div>
  );
}
