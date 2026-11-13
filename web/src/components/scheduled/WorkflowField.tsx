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
 * for each run to follow. Its menu is as wide as the field, so a long
 * description is cut off as in the composer's Run a workflow list. An
 * automation created from a workflow shows it `locked`.
 */
export function WorkflowField({
  workflows,
  value,
  onChange,
  onOpenChange,
  locked = false,
}: {
  workflows: readonly SavedWorkflow[];
  value: SavedWorkflow | null;
  onChange: (workflow: SavedWorkflow | null) => void;
  /** Reports the menu opening and closing, as the dialog's other pickers do. */
  onOpenChange?: (open: boolean) => void;
  /** Shows `value` with no menu, so it can't be changed or cleared. */
  locked?: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (locked && value) {
    return (
      <div className="flex flex-col gap-1.5" data-testid="task-workflow-field">
        <Label>Workflow</Label>
        {/* The chosen field's look, in full color, without its chevron or menu. */}
        <Button
          type="button"
          variant="outline"
          disabled
          className="w-full justify-start gap-2 text-foreground disabled:opacity-100"
          data-testid="task-workflow-trigger"
        >
          <WorkflowIcon className="size-3.5 shrink-0" />
          <span className="flex-1 truncate text-left">{value.name}</span>
        </Button>
      </div>
    );
  }
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
              "w-full justify-start gap-2",
              value ? "text-foreground" : "text-muted-foreground",
              pickerTriggerClass,
            )}
            data-testid="task-workflow-trigger"
          >
            {value && <WorkflowIcon className="size-3.5 shrink-0" />}
            <span className="flex-1 truncate text-left">{value?.name ?? "No workflow"}</span>
            <ChevronDownIcon className="size-4 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-[var(--radix-dropdown-menu-trigger-width)]">
          <WorkflowPicker
            workflows={workflows}
            heading={null}
            selectedId={value?.id}
            onSelect={onChange}
            onClear={value ? () => onChange(null) : undefined}
          />
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
