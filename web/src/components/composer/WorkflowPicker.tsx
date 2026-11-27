import { useRef, useState } from "react";
import { CheckIcon, PlusIcon, SearchIcon, WorkflowIcon, XIcon } from "lucide-react";

import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { enabledItems } from "@/components/ui/menu-filter";
import type { SavedWorkflow, WorkflowPagination } from "@/hooks/useSavedWorkflows";
import { isImeCompositionKeyEvent } from "@/lib/ime";

/**
 * Whether Run a workflow has saved workflows to list, rather than going straight to
 * describing a new one. Drafts can't run, so the list leaves them out.
 */
export function offersSavedWorkflows(
  workflows: readonly SavedWorkflow[],
  pagination?: Pick<WorkflowPagination, "hasMore">,
): boolean {
  // A list whose loaded pages show nothing yet can still have more to load.
  return workflows.some((workflow) => !workflow.is_draft) || pagination?.hasMore === true;
}

/**
 * Searchable list of the user's saved workflows, and any shared with them, laid out
 * like `ProjectPicker`'s "Search or create project" list. The "+" menu shows it in place of its items
 * for Run a workflow, and `RunWorkflowMenu` in its own menu; New automation shows it in its
 * Workflow field's menu.
 *
 * Picking one calls `onSelect(workflow)`. Below the list, "New workflow" calls
 * `onDescribe(text)` with whatever was typed, and "No workflow" calls `onClear()`.
 */
export function WorkflowPicker({
  workflows,
  pagination,
  onSelect,
  onDescribe,
  onClear,
  selectedId,
  heading = "Run a workflow",
}: {
  workflows: readonly SavedWorkflow[];
  pagination?: WorkflowPagination;
  onSelect: (workflow: SavedWorkflow) => void;
  onDescribe?: (description: string) => void;
  onClear?: () => void;
  /** The workflow already chosen, which gets a check mark. */
  selectedId?: string;
  /** The list's heading; `null` for none. */
  heading?: string | null;
}) {
  const [search, setSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const trimmed = search.trim();
  const filtered = workflows.filter(
    (workflow) => !workflow.is_draft && workflow.name.toLowerCase().includes(trimmed.toLowerCase()),
  );

  return (
    <div
      className="flex flex-col gap-0.5"
      data-testid="composer-workflow-picker"
      // Up from the first item goes back to the search.
      onKeyDownCapture={(event) => {
        if (event.key !== "ArrowUp" || event.target !== enabledItems(searchRef.current)[0]) return;
        event.preventDefault();
        event.stopPropagation();
        searchRef.current?.focus();
      }}
    >
      {heading && (
        <div className="px-2 py-1 text-xs leading-4 text-muted-foreground">{heading}</div>
      )}
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <input
          ref={searchRef}
          aria-label="Search workflows"
          className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          placeholder="Search workflows"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          // Typing filters the list rather than reaching the menu's typeahead. Tab stays in
          // the menu, and ArrowDown moves to the first item.
          onKeyDown={(event) => {
            if (event.key !== "Tab") event.stopPropagation();
            if (event.key === "ArrowDown" && !isImeCompositionKeyEvent(event)) {
              event.preventDefault();
              enabledItems(event.currentTarget)[0]?.focus();
            }
          }}
          autoFocus
        />
      </div>
      <div className="max-h-64 overflow-y-auto">
        {filtered.map((workflow) => (
          <DropdownMenuItem
            key={workflow.id}
            className="mobile-touch-target"
            textValue={workflow.name}
            data-testid="composer-saved-workflow"
            onSelect={() => onSelect(workflow)}
          >
            <span className="flex size-4 shrink-0 items-center justify-center">
              <WorkflowIcon className="size-3.5" />
            </span>
            {/* One line: the description is cut off to fit, so the row never widens. */}
            <span className="flex min-w-0 flex-1 items-center gap-2">
              <span className="max-w-[70%] shrink-0 truncate">{workflow.name}</span>
              <span className="min-w-0 flex-1 truncate text-xs leading-4 text-muted-foreground">
                {workflow.viewer_role === "viewer" && (
                  <span data-testid="composer-saved-workflow-owner">
                    Shared by {workflow.owner_display_name ?? "someone else"} ·{" "}
                  </span>
                )}
                {workflow.description.split("\n")[0]}
              </span>
            </span>
            {workflow.id === selectedId && <CheckIcon className="size-3.5 shrink-0 text-primary" />}
          </DropdownMenuItem>
        ))}
        {filtered.length === 0 && (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">No matching workflows</p>
        )}
        {pagination?.hasMore && (
          <DropdownMenuItem
            disabled={pagination.loadingMore}
            onSelect={(event) => {
              event.preventDefault();
              pagination.loadMore();
            }}
          >
            {pagination.loadingMore ? "Loading…" : "Load more workflows"}
          </DropdownMenuItem>
        )}
      </div>
      {(onDescribe || onClear) && (
        <div className="border-t pt-1">
          {onDescribe && (
            <DropdownMenuItem
              className="mobile-touch-target"
              data-testid="composer-describe-workflow"
              onSelect={() => onDescribe(trimmed)}
            >
              <span className="flex size-4 shrink-0 items-center justify-center">
                <PlusIcon className="size-3.5" />
              </span>
              New workflow
            </DropdownMenuItem>
          )}
          {onClear && (
            <DropdownMenuItem
              className="mobile-touch-target"
              data-testid="workflow-picker-clear"
              onSelect={() => onClear()}
            >
              <span className="flex size-4 shrink-0 items-center justify-center">
                <XIcon className="size-3.5" />
              </span>
              No workflow
            </DropdownMenuItem>
          )}
        </div>
      )}
    </div>
  );
}
