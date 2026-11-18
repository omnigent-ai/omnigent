/**
 * Workflows page (`/workflows`): the user's saved workflows, those shared with them,
 * a search, and a "New workflow" button. Laid out like the Automations page.
 *
 * New workflow saves a draft and opens its plan editor; Run asks where a saved one's session runs. Create
 * automation opens New automation with the workflow chosen and locked. Delete
 * removes one. A shared workflow offers Run and Clone; only its owner changes it.
 */

import { useMemo, useState } from "react";
import { Loader2Icon, SearchIcon, TriangleAlertIcon, WorkflowIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { NewWorkflowDialog } from "@/components/workflows/NewWorkflowDialog";
import { DeleteWorkflowDialog } from "@/components/workflows/DeleteWorkflowDialog";
import { WorkflowRow } from "@/components/workflows/WorkflowRow";
import {
  type WorkflowSessionRequest,
  WorkflowSessionDialog,
} from "@/components/workflows/WorkflowSessionDialog";
import {
  type SavedWorkflow,
  useSavedWorkflows,
  useSharedWorkflows,
} from "@/hooks/useSavedWorkflows";
import { useAutomationsEnabled } from "@/hooks/useConversations";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { useCloneWorkflow, useCreateWorkflowDraft } from "@/hooks/useWorkflow";

export function WorkflowsPage() {
  const { workflows, isLoading, isError, refresh, pagination } = useSavedWorkflows();
  const shared = useSharedWorkflows();
  const createDraft = useCreateWorkflowDraft();
  const cloneWorkflow = useCloneWorkflow();
  const navigate = useNavigate();
  const info = useServerInfo();
  const automationsEnabled = useAutomationsEnabled();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SavedWorkflow | null>(null);
  // The request stays set while its session dialog closes.
  const [sessionRequest, setSessionRequest] = useState<WorkflowSessionRequest | null>(null);
  const [sessionOpen, setSessionOpen] = useState(false);
  const startSession = (request: WorkflowSessionRequest) => {
    setSessionRequest(request);
    setSessionOpen(true);
  };
  // Likewise for its New automation dialog.
  const [automationTarget, setAutomationTarget] = useState<SavedWorkflow | null>(null);
  const [automationOpen, setAutomationOpen] = useState(false);

  const hasAnyWorkflows = workflows.length > 0 || shared.workflows.length > 0;
  const query = search.trim().toLowerCase();
  const filtered = useMemo(() => matching(workflows, query), [workflows, query]);
  const sharedFiltered = useMemo(
    () => matching(shared.workflows, query),
    [shared.workflows, query],
  );
  const clone = (workflow: SavedWorkflow) =>
    cloneWorkflow.mutate(workflow.id, {
      onSuccess: (copy) => navigate(`/workflows/${copy.id}`),
      onError: (error) => showToast(`Couldn’t clone workflow. ${error.message}`),
    });
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
        <LoadError
          message="Couldn’t load workflows."
          onRetry={refresh}
          testId="workflows-load-error"
          componentId="workflows.retry"
        />
      ) : isLoading ? (
        <div className="flex items-center gap-2 py-12 text-ui text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading workflows…
        </div>
      ) : filtered.length === 0 ? (
        // Wait for the shared list too, which may yet have workflows.
        sharedFiltered.length === 0 &&
        !shared.isLoading &&
        !shared.isError &&
        !shared.pagination.hasMore && <EmptyState hasAny={hasAnyWorkflows} />
      ) : (
        <div className="flex flex-col gap-2" data-testid="workflows-list">
          {filtered.map((workflow) => (
            <WorkflowRow
              key={workflow.id}
              workflow={workflow}
              onRun={() => startSession({ kind: "run", workflow })}
              onCreateAutomation={
                automationsEnabled
                  ? () => {
                      setAutomationTarget(workflow);
                      setAutomationOpen(true);
                    }
                  : undefined
              }
              onClone={clone}
              cloning={cloneWorkflow.isPending}
              onDelete={() => setDeleteTarget(workflow)}
            />
          ))}
        </div>
      )}

      {pagination?.hasMore && (
        <Button
          variant="outline"
          className="mt-4"
          disabled={pagination.loadingMore}
          onClick={pagination.loadMore}
          componentId="workflows.load-more"
        >
          {pagination.loadingMore ? "Loading…" : "Load more workflows"}
        </Button>
      )}

      {shared.isError ? (
        <LoadError
          className="mt-8"
          message="Couldn’t load the workflows shared with you."
          onRetry={shared.refresh}
          testId="workflows-shared-load-error"
          componentId="workflows.shared.retry"
        />
      ) : (
        // Shown while more can load, even when no loaded workflow matches.
        (sharedFiltered.length > 0 || shared.pagination.hasMore) && (
          <section className="mt-8" data-testid="workflows-shared">
            <h2 className="mb-2 text-sm font-medium text-muted-foreground">Shared with you</h2>
            <div className="flex flex-col gap-2">
              {sharedFiltered.map((workflow) => (
                <WorkflowRow
                  key={workflow.id}
                  workflow={workflow}
                  onRun={() => startSession({ kind: "run", workflow })}
                  onClone={clone}
                  cloning={cloneWorkflow.isPending}
                />
              ))}
            </div>
            {shared.pagination.hasMore && (
              <Button
                variant="outline"
                className="mt-4"
                disabled={shared.pagination.loadingMore}
                onClick={shared.pagination.loadMore}
                componentId="workflows.shared.load-more"
              >
                {shared.pagination.loadingMore ? "Loading…" : "Load more shared workflows"}
              </Button>
            )}
          </section>
        )
      )}

      {deleteTarget && (
        <DeleteWorkflowDialog workflow={deleteTarget} onClose={() => setDeleteTarget(null)} />
      )}
      <NewWorkflowDialog
        open={creating}
        onOpenChange={setCreating}
        onCreate={async (draft) => {
          const workflow = await createDraft.mutateAsync(draft);
          navigate(`/workflows/${workflow.id}`);
        }}
        takenNames={takenNames}
      />
      <WorkflowSessionDialog
        request={sessionRequest}
        open={sessionOpen}
        onOpenChange={setSessionOpen}
      />
      {automationsEnabled && automationTarget && (
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

/** A list that failed to load, and a Retry for it. */
function LoadError({
  message,
  onRetry,
  testId,
  componentId,
  className,
}: {
  message: string;
  onRetry: () => void;
  testId: string;
  componentId: string;
  className?: string;
}) {
  return (
    <div
      role="alert"
      data-testid={testId}
      className={cn(
        "flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui",
        className,
      )}
    >
      <TriangleAlertIcon className="size-4 shrink-0 text-destructive" />
      <span className="flex-1">{message}</span>
      <Button variant="outline" size="sm" onClick={onRetry} componentId={componentId}>
        Retry
      </Button>
    </div>
  );
}

/** The workflows whose name or description contains *query*, already lowercased. */
function matching(workflows: readonly SavedWorkflow[], query: string): readonly SavedWorkflow[] {
  if (!query) return workflows;
  return workflows.filter(
    (workflow) =>
      workflow.name.toLowerCase().includes(query) ||
      workflow.description.toLowerCase().includes(query),
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
