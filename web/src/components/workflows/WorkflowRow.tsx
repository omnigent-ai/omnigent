import type { ComponentType, ReactNode } from "react";
import { ClockIcon, MoreHorizontalIcon, PencilIcon, PlayIcon, Trash2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { WorkflowSummary } from "@/hooks/useWorkflows";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { cn } from "@/lib/utils";

export interface WorkflowRowProps {
  workflow: WorkflowSummary;
  onRun: (workflow: WorkflowSummary) => void;
  onEdit: (workflow: WorkflowSummary) => void;
  onCreateAutomation: (workflow: WorkflowSummary) => void;
  onDelete: (workflow: WorkflowSummary) => void;
}

/**
 * One saved workflow: its name and description, with Run, Edit, Create
 * automation and Delete in a ⋯ menu and on right-click, like an automation's row.
 */
export function WorkflowRow({
  workflow,
  onRun,
  onEdit,
  onCreateAutomation,
  onDelete,
}: WorkflowRowProps) {
  const actions = { workflow, onRun, onEdit, onCreateAutomation, onDelete };

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          data-testid="workflow-row"
          className="group relative flex items-center gap-3 rounded-xl border border-border bg-card py-3 pr-12 pl-4"
        >
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="truncate text-[15px] font-semibold">{workflow.name}</span>
            <span className="line-clamp-2 text-ui text-muted-foreground/80">
              {workflow.description}
            </span>
          </div>
          {/* Hidden until the row is hovered or focused on desktop, as on automation rows. */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={`Actions for ${workflow.name}`}
                data-testid="workflow-row-menu"
                className={cn(
                  "-translate-y-1/2 absolute top-1/2 right-3 transition-opacity",
                  "md:opacity-0 md:group-hover:opacity-100 md:group-has-[:focus-visible]:opacity-100",
                  "md:aria-expanded:opacity-100",
                )}
              >
                <MoreHorizontalIcon className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <WorkflowMenuItems Item={DropdownMenuItem} {...actions} />
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent className="min-w-40">
        <WorkflowMenuItems Item={ContextMenuItem} {...actions} />
      </ContextMenuContent>
    </ContextMenu>
  );
}

// Item props shared by the dropdown and context-menu item primitives.
interface MenuItemProps {
  children?: ReactNode;
  variant?: "default" | "destructive";
  onSelect?: (event: Event) => void;
  "data-testid"?: string;
}

// The actions, written once for both the ⋯ menu and the right-click menu.
function WorkflowMenuItems({
  Item,
  workflow,
  onRun,
  onEdit,
  onCreateAutomation,
  onDelete,
}: {
  Item: ComponentType<MenuItemProps>;
  workflow: WorkflowSummary;
  onRun: (workflow: WorkflowSummary) => void;
  onEdit: (workflow: WorkflowSummary) => void;
  onCreateAutomation: (workflow: WorkflowSummary) => void;
  onDelete: (workflow: WorkflowSummary) => void;
}) {
  const { trackClick } = useOmnigentAnalytics();
  return (
    <>
      <Item
        onSelect={() => {
          trackClick("workflows.row.run", "button");
          onRun(workflow);
        }}
        data-testid="workflow-run"
      >
        <PlayIcon className="size-4" />
        Run
      </Item>
      <Item
        onSelect={() => {
          trackClick("workflows.row.edit", "button");
          onEdit(workflow);
        }}
        data-testid="workflow-edit"
      >
        <PencilIcon className="size-4" />
        Edit
      </Item>
      <Item
        onSelect={() => {
          trackClick("workflows.row.create_automation", "button");
          onCreateAutomation(workflow);
        }}
        data-testid="workflow-create-automation"
      >
        <ClockIcon className="size-4" />
        Create automation
      </Item>
      <Item
        variant="destructive"
        onSelect={() => {
          trackClick("workflows.row.delete", "button");
          onDelete(workflow);
        }}
        data-testid="workflow-delete"
      >
        <Trash2Icon className="size-4" />
        Delete
      </Item>
    </>
  );
}
