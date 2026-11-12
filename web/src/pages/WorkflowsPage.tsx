/**
 * Workflows page (`/workflows`): the user's saved workflows, a search, and a
 * "New workflow" button. Laid out like the Automations page.
 *
 * Data comes from `useWorkflows`. Creating a workflow will start a session
 * where the agent drafts it; that isn't connected yet.
 */

import { useMemo, useState } from "react";
import { Loader2Icon, SearchIcon, TriangleAlertIcon, WorkflowIcon } from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { showToast } from "@/components/ui/toast";
import { NewWorkflowDialog } from "@/components/workflows/NewWorkflowDialog";
import { useNow } from "@/hooks/useNow";
import { type WorkflowSummary, useWorkflows } from "@/hooks/useWorkflows";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";

export function WorkflowsPage() {
  const { data: workflows, isLoading, isError, refetch } = useWorkflows();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const now = useNow();

  const hasAnyWorkflows = (workflows?.length ?? 0) > 0;
  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    const all = workflows ?? [];
    if (!query) return all;
    return all.filter(
      (workflow) =>
        workflow.name.toLowerCase().includes(query) ||
        workflow.description.toLowerCase().includes(query),
    );
  }, [workflows, search]);
  const takenNames = useMemo(() => (workflows ?? []).map((workflow) => workflow.name), [workflows]);

  return (
    <PageScroll className="[scrollbar-gutter:stable]" contentClassName="px-4 md:px-6">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Workflows</h1>
          <p className="text-ui text-muted-foreground">
            Run multi-step work again from a saved plan.
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
          <Button variant="outline" size="sm" onClick={refetch} componentId="workflows.retry">
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
            <WorkflowRow key={workflow.id} workflow={workflow} now={now} />
          ))}
        </div>
      )}

      <NewWorkflowDialog
        open={creating}
        onOpenChange={setCreating}
        onCreate={() => showToast("Creating workflows isn’t connected yet.")}
        takenNames={takenNames}
      />
    </PageScroll>
  );
}

function WorkflowRow({ workflow, now }: { workflow: WorkflowSummary; now: Date }) {
  const updatedMs = workflow.updated_at * 1000;
  const ago = relativeTime(updatedMs, now.getTime());
  return (
    <div
      data-testid="workflow-row"
      className="flex items-baseline gap-4 rounded-xl border border-border bg-card px-4 py-3"
    >
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="truncate text-[15px] font-semibold">{workflow.name}</span>
        <span className="line-clamp-2 text-ui text-muted-foreground/80">
          {workflow.description}
        </span>
      </div>
      <time
        dateTime={new Date(updatedMs).toISOString()}
        title={absoluteTime(updatedMs)}
        className="shrink-0 text-sm text-muted-foreground"
        data-testid="workflow-row-updated"
      >
        {ago === "now" ? "Updated just now" : `Updated ${ago} ago`}
      </time>
    </div>
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
