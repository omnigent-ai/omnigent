// A group header's actions, such as hiding a repository's group: in its ellipsis menu and its
// context menu, from one list so both offer the same. One that can't run says why under it.

import type { ReactElement } from "react";
import { EllipsisIcon } from "lucide-react";

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { GroupAction } from "@/shell/usePullRequestListEntries";
import { DescribedMenuItem } from "@/pullRequestPage/describedMenuItems";

/** The header with its context menu, or the header alone where it offers nothing. */
export function PullRequestListGroupMenu({
  actions,
  children,
}: {
  actions: readonly GroupAction[];
  children: ReactElement;
}) {
  if (actions.length === 0) return children;
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent>
        {actions.map((action) => (
          <ContextMenuItem
            key={action.id}
            disabled={!!action.unavailableReason}
            description={action.unavailableReason}
            onSelect={action.run}
          >
            <action.icon aria-hidden />
            {action.label}
          </ContextMenuItem>
        ))}
      </ContextMenuContent>
    </ContextMenu>
  );
}

/** The header's ellipsis button, shown on hover or focus like its Remove All. */
export function PullRequestListGroupActionsButton({
  actions,
  groupTitle,
}: {
  actions: readonly GroupAction[];
  groupTitle: string;
}) {
  if (actions.length === 0) return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="pr-list-group-action pr-list-group-more"
          aria-label={`More actions for ${groupTitle}`}
          title="More Actions"
        >
          <EllipsisIcon aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {actions.map((action) => (
          <DescribedMenuItem
            key={action.id}
            disabled={!!action.unavailableReason}
            description={action.unavailableReason}
            onSelect={action.run}
          >
            <action.icon aria-hidden />
            {action.label}
          </DescribedMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
