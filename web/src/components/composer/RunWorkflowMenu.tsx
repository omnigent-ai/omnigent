import { type ReactNode, useRef } from "react";

import { offersSavedWorkflows, WorkflowPicker } from "@/components/composer/WorkflowPicker";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { type SavedWorkflow, useSavedWorkflows } from "@/hooks/useSavedWorkflows";

/**
 * Run a workflow from outside the composer, e.g. the Tasks tab's empty state.
 * Like the "+" menu's Run a workflow, the button lists the user's saved workflows
 * to run or a new one to describe, and goes straight to describing one when none
 * can run.
 *
 * @param onStart - Opens the workflow dialog: with no arguments to describe one,
 *   with the search text for New workflow, or with the saved workflow picked. A
 *   choice from the menu runs once the menu has closed, so the dialog keeps focus.
 */
export function RunWorkflowMenu({
  className,
  children,
  onStart,
}: {
  className?: string;
  children: ReactNode;
  onStart: (description?: string, savedWorkflow?: SavedWorkflow) => void;
}) {
  const { workflows, pagination, refresh } = useSavedWorkflows({ includeShared: true });
  const pendingActionRef = useRef<(() => void) | null>(null);
  if (!offersSavedWorkflows(workflows, pagination)) {
    return (
      <button type="button" className={className} onClick={() => onStart()}>
        {children}
      </button>
    );
  }
  return (
    <DropdownMenu
      onOpenChange={(open) => {
        // A workflow saved since the list last loaded shows up.
        if (open) refresh();
      }}
    >
      <DropdownMenuTrigger asChild>
        <button type="button" className={className}>
          {children}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        onCloseAutoFocus={() => {
          const action = pendingActionRef.current;
          pendingActionRef.current = null;
          if (action !== null) requestAnimationFrame(action);
        }}
        collisionPadding={12}
        aria-label="Run a workflow"
        // The "+" menu's look, so the list reads the same in both places.
        className="composer-add-menu w-80 max-w-[calc(100vw-24px)] rounded-[16px] max-md:p-1.5"
      >
        <WorkflowPicker
          workflows={workflows}
          pagination={pagination}
          onSelect={(workflow) => {
            pendingActionRef.current = () => onStart("", workflow);
          }}
          onDescribe={(description) => {
            pendingActionRef.current = () => onStart(description);
          }}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
