/**
 * Tasks / Work-Items page (``/tasks``).
 *
 * Lists work items from ``/v1/work-items`` (agent- or externally-created),
 * lets you change a task's status, open the conversation working on it, or
 * delete it. Reached from the left-pane Tasks button (below Inbox).
 */

import { ExternalLinkIcon, RefreshCwIcon, TrashIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Link } from "@/lib/routing";
import { WORK_ITEM_STATUSES, type WorkItem } from "@/lib/workItemsApi";
import { useDeleteWorkItem, useUpdateWorkItem, useWorkItems } from "@/hooks/useWorkItems";

function TaskRow({ item }: { item: WorkItem }) {
  const update = useUpdateWorkItem();
  const remove = useDeleteWorkItem();

  return (
    <li className="flex items-center gap-3 rounded-lg border px-3 py-2" data-testid="task-row">
      <select
        aria-label="Status"
        className="shrink-0 rounded-md border bg-background px-2 py-1 text-xs"
        value={item.status}
        onChange={(e) => update.mutate({ id: item.id, patch: { status: e.target.value } })}
        disabled={update.isPending}
      >
        {WORK_ITEM_STATUSES.map((s) => (
          <option key={s} value={s}>
            {s.replace(/_/g, " ")}
          </option>
        ))}
      </select>

      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">
          {item.conversation_id ? (
            <Link className="hover:underline" to={`/c/${item.conversation_id}`}>
              {item.title}
            </Link>
          ) : (
            item.title
          )}
        </div>
        <div className="truncate text-xs text-muted-foreground">
          {item.source}
          {item.external_id ? ` · ${item.external_id}` : ""}
        </div>
      </div>

      {item.pr_url && (
        <a
          href={item.pr_url}
          target="_blank"
          rel="noreferrer"
          className="shrink-0 text-muted-foreground hover:text-foreground"
          aria-label="Open pull request"
        >
          <ExternalLinkIcon className="size-4" />
        </a>
      )}

      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Delete task"
        onClick={() => remove.mutate(item.id)}
        disabled={remove.isPending}
      >
        <TrashIcon className="size-4" />
      </Button>
    </li>
  );
}

export function TasksPage() {
  const { data: items, isLoading, isError, refetch, isFetching } = useWorkItems();

  return (
    <PageScroll>
      <div className="mx-auto w-full max-w-3xl px-4 py-6">
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-lg font-semibold">Tasks</h1>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Refresh tasks"
            onClick={() => void refetch()}
            disabled={isFetching}
          >
            <RefreshCwIcon className="size-4" />
          </Button>
        </div>

        {isLoading && <p className="text-sm text-muted-foreground">Loading tasks…</p>}
        {isError && <p className="text-sm text-destructive">Failed to load tasks.</p>}
        {items && items.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No tasks yet. Agents create them with <code>create_work_item</code>, or push them in
            from Slack/email/GitHub/Jira.
          </p>
        )}
        {items && items.length > 0 && (
          <ul className="flex flex-col gap-2">
            {items.map((item) => (
              <TaskRow key={item.id} item={item} />
            ))}
          </ul>
        )}
      </div>
    </PageScroll>
  );
}

export default TasksPage;
