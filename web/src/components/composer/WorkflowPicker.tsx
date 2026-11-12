import { useState } from "react";
import { CheckIcon, PlusIcon, SearchIcon, WorkflowIcon, XIcon } from "lucide-react";

import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";

/**
 * Searchable list of the user's saved workflows, laid out like `ProjectPicker`'s
 * "Search or create project" list. The "+" menu shows it in place of its items
 * for Run a workflow; New automation shows it in its Workflow field's menu.
 *
 * Picking one calls `onSelect(workflow)`. Below the list, "New workflow" calls
 * `onDescribe(text)` with whatever was typed, and "No workflow" calls `onClear()`.
 */
export function WorkflowPicker({
  workflows,
  onSelect,
  onDescribe,
  onClear,
  selectedId,
  heading = "Run a workflow",
}: {
  workflows: readonly SavedWorkflow[];
  onSelect: (workflow: SavedWorkflow) => void;
  onDescribe?: (description: string) => void;
  onClear?: () => void;
  /** The workflow already chosen, which gets a check mark. */
  selectedId?: string;
  /** The list's heading; `null` for none. */
  heading?: string | null;
}) {
  const [search, setSearch] = useState("");
  const trimmed = search.trim();
  const filtered = trimmed
    ? workflows.filter((workflow) => workflow.name.toLowerCase().includes(trimmed.toLowerCase()))
    : workflows;

  return (
    <div className="flex flex-col gap-0.5" data-testid="composer-workflow-picker">
      {heading && (
        <div className="px-2 py-1 text-xs leading-4 text-muted-foreground">{heading}</div>
      )}
      <div className="flex items-center gap-2 border-b px-2 py-1.5">
        <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <input
          aria-label="Search workflows"
          className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          placeholder="Search workflows"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          // Typing filters the list rather than jumping between menu items.
          onKeyDown={(event) => event.stopPropagation()}
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
                {workflow.description.split("\n")[0]}
              </span>
            </span>
            {workflow.id === selectedId && <CheckIcon className="size-3.5 shrink-0 text-primary" />}
          </DropdownMenuItem>
        ))}
        {filtered.length === 0 && (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">No matching workflows</p>
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
