// The sidebar menu's saved views: choosing one shows the sidebar as it says, and the sidebar as
// it is can be saved, or the view it matches deleted.

import {
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import type { SidebarSavedView } from "@/lib/sidebarSavedViews";

export function SidebarSavedViewItems({
  views,
  matched,
  onApply,
  onSave,
  onDelete,
}: {
  views: readonly SidebarSavedView[];
  /** The view the sidebar shows now, if any. */
  matched: SidebarSavedView | null;
  onApply: (view: SidebarSavedView) => void;
  onSave: () => void;
  onDelete: (view: SidebarSavedView) => void;
}) {
  return (
    <>
      {views.length > 0 && (
        <>
          <DropdownMenuRadioGroup
            value={matched?.id ?? ""}
            onValueChange={(id) => {
              const view = views.find((candidate) => candidate.id === id);
              if (view) onApply(view);
            }}
          >
            {views.map((view) => (
              <DropdownMenuRadioItem
                className="mobile-touch-target"
                key={view.id}
                value={view.id}
                data-testid="sidebar-saved-view"
              >
                <span className="truncate">{view.name}</span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
        </>
      )}
      <DropdownMenuItem
        className="mobile-touch-target"
        data-testid="sidebar-saved-view-save"
        onSelect={onSave}
      >
        Save current view…
      </DropdownMenuItem>
      {matched && (
        <DropdownMenuItem
          className="mobile-touch-target"
          variant="destructive"
          data-testid="sidebar-saved-view-delete"
          onSelect={() => onDelete(matched)}
        >
          <span className="truncate">Delete “{matched.name}”</span>
        </DropdownMenuItem>
      )}
    </>
  );
}
