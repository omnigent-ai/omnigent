import { useState } from "react";
import { PlusIcon, SearchIcon, WorkflowIcon } from "lucide-react";

import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";

/**
 * Searchable list of the user's saved workflows, shown in place of the "+"
 * menu's items when Run a workflow has saved ones to offer. Picking one calls
 * `onSelect(workflow)`; the last row, "Describe a new workflow…", calls
 * `onDescribe(text)` with whatever was typed, for the description dialog.
 *
 * Laid out like `ProjectPicker`'s "Search or create project" list.
 */
export function WorkflowPicker({
  workflows,
  onSelect,
  onDescribe,
}: {
  workflows: readonly SavedWorkflow[];
  onSelect: (workflow: SavedWorkflow) => void;
  onDescribe: (description: string) => void;
}) {
  const [search, setSearch] = useState("");
  const trimmed = search.trim();
  const filtered = trimmed
    ? workflows.filter((workflow) => workflow.name.toLowerCase().includes(trimmed.toLowerCase()))
    : workflows;

  return (
    <div className="flex flex-col gap-0.5" data-testid="composer-workflow-picker">
      <div className="px-2 py-1 text-xs leading-4 text-muted-foreground">Run a workflow</div>
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
            <span className="flex h-lh min-w-0 flex-wrap items-center gap-x-2 overflow-hidden">
              <span className="min-w-0 truncate">{workflow.name}</span>
              <span className="text-xs leading-4 text-muted-foreground">
                {workflow.description.split("\n")[0]}
              </span>
            </span>
          </DropdownMenuItem>
        ))}
        {filtered.length === 0 && (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">No matching workflows</p>
        )}
      </div>
      <div className="border-t pt-1">
        <DropdownMenuItem
          className="mobile-touch-target"
          data-testid="composer-describe-workflow"
          onSelect={() => onDescribe(trimmed)}
        >
          <span className="flex size-4 shrink-0 items-center justify-center">
            <PlusIcon className="size-3.5" />
          </span>
          Describe a new workflow…
        </DropdownMenuItem>
      </div>
    </div>
  );
}
