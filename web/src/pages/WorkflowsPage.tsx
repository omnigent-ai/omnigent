/**
 * Workflows page (`/workflows`): the user's saved workflows, a search, and a
 * "New workflow" button. Laid out like the Automations page.
 *
 * New workflow starts a session, from the new-session page's defaults, that asks
 * the agent to draft and save the workflow. Run opens a dialog to pick where and
 * how the workflow's session runs. Create automation opens New automation with
 * the workflow chosen and locked. Delete removes one.
 */

import { useMemo, useState } from "react";
import { Loader2Icon, SearchIcon, TriangleAlertIcon, WorkflowIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { NewWorkflowDialog } from "@/components/workflows/NewWorkflowDialog";
import { RunWorkflowDialog } from "@/components/workflows/RunWorkflowDialog";
import { WorkflowRow } from "@/components/workflows/WorkflowRow";
import {
  type SavedWorkflow,
  useDeleteWorkflow,
  useSavedWorkflows,
} from "@/hooks/useSavedWorkflows";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { newSessionLocation } from "@/lib/newSessionHandoff";
import { useNavigate } from "@/lib/routing";
import { createWorkflowPrompt } from "@/lib/workflow";

export function WorkflowsPage() {
  const { workflows, isLoading, isError, refresh } = useSavedWorkflows();
  const deleteWorkflow = useDeleteWorkflow();
  const navigate = useNavigate();
  const info = useServerInfo();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  // The workflow stays set while its Run dialog closes.
  const [runTarget, setRunTarget] = useState<SavedWorkflow | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  // Likewise for its New automation dialog.
  const [automationTarget, setAutomationTarget] = useState<SavedWorkflow | null>(null);
  const [automationOpen, setAutomationOpen] = useState(false);

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
              onRun={() => {
                setRunTarget(workflow);
                setRunOpen(true);
              }}
              onCreateAutomation={() => {
                setAutomationTarget(workflow);
                setAutomationOpen(true);
              }}
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
          navigate(newSessionLocation(createWorkflowPrompt(name, description), [], { start: true }))
        }
        takenNames={takenNames}
      />
      <RunWorkflowDialog workflow={runTarget} open={runOpen} onOpenChange={setRunOpen} />
      {automationTarget && (
        <CreateScheduledTaskDialog
          open={automationOpen}
          onOpenChange={setAutomationOpen}
          initialName={automationTarget.name}
          lockedWorkflow={automationTarget}
          onCreated={(task) => {
            showToast("Automation created");
            navigate(automationPagePath(info, task));
          }}
        />
      )}
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
            Create a workflow to get started.
          </p>
        </div>
      )}
    </div>
  );
}
