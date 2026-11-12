import { useState } from "react";
import { ChevronDownIcon, WorkflowIcon } from "lucide-react";

import { WorkflowPicker } from "@/components/composer/WorkflowPicker";
import { Label } from "@/components/scheduled/Label";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { cn } from "@/lib/utils";
import { pickerTriggerClass } from "./pickerStyles";

/**
 * New automation's optional Workflow field: one of the user's saved workflows
 * for each run to follow. Laid out like the dialog's Project field; the
 * description shows three lines, and its tooltip has the rest.
 */
export function WorkflowField({
  workflows,
  value,
  onChange,
  onOpenChange,
}: {
  workflows: readonly SavedWorkflow[];
  value: SavedWorkflow | null;
  onChange: (workflow: SavedWorkflow | null) => void;
  /** Reports the menu opening and closing, as the dialog's other pickers do. */
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-1.5" data-testid="task-workflow-field">
      <Label>Workflow (optional)</Label>
      <DropdownMenu
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          onOpenChange?.(next);
        }}
      >
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            className={cn(
              "w-full justify-start gap-2 border-0 shadow-none text-muted-foreground",
              pickerTriggerClass,
            )}
            data-testid="task-workflow-trigger"
          >
            {value && <WorkflowIcon className="size-3.5 shrink-0" />}
            <span className="flex-1 truncate text-left">{value?.name ?? "Choose a workflow"}</span>
            <ChevronDownIcon className="size-4 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-56">
          <WorkflowPicker
            workflows={workflows}
            heading={null}
            selectedId={value?.id}
            onSelect={onChange}
            onClear={value ? () => onChange(null) : undefined}
          />
        </DropdownMenuContent>
      </DropdownMenu>
      {value?.description && (
        <p
          className="line-clamp-3 whitespace-pre-line text-sm text-muted-foreground"
          title={value.description}
          data-testid="task-workflow-description"
        >
          {value.description}
        </p>
      )}
    </div>
  );
}
