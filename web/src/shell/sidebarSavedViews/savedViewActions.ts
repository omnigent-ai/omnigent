// What saving and deleting a sidebar view say, shared by the sidebar menu, ⌘K and the dialog.

import { toast } from "sonner";

import {
  deleteView,
  SAVED_VIEW_LIMIT,
  type SavedViewScope,
  type SaveViewProblem,
  type SidebarSavedView,
} from "@/lib/sidebarSavedViews";

/** Why a name can't be saved, as the save dialog says it. */
export const SAVE_VIEW_PROBLEM_TEXT: Record<SaveViewProblem, string> = {
  empty: "Name the view to save it.",
  duplicate: "A view with this name already exists.",
  limit: `You can keep up to ${SAVED_VIEW_LIMIT} views. Delete one to save another.`,
  storage: "Couldn't save the view on this device. Try again.",
};

/** Why a deleted view couldn't come back; it stays deleted, and no other view changes. */
export function restoreProblemText(
  name: string,
  problem: "duplicate" | "limit" | "storage",
): string {
  switch (problem) {
    case "duplicate":
      return `Couldn't bring back “${name}”: a view with this name already exists.`;
    case "limit":
      return `Couldn't bring back “${name}”: you already keep ${SAVED_VIEW_LIMIT} views. Delete one first.`;
    case "storage":
      return "Couldn't restore the view. Try again.";
  }
}

/** How long the Undo after a delete stays up: long enough to catch a mis-click. */
const DELETE_UNDO_DURATION_MS = 5000;

/** Deletes `view`, offering Undo; it says so when storage refuses either change. */
export function deleteViewWithUndo(scope: SavedViewScope, view: SidebarSavedView): void {
  const restore = deleteView(scope, view.id);
  if (!restore) {
    toast.error("Couldn't delete the view. Try again.");
    return;
  }
  toast(`Deleted the “${view.name}” view`, {
    id: `sidebar-view-deleted:${view.id}`,
    duration: DELETE_UNDO_DURATION_MS,
    action: {
      label: "Undo",
      onClick: () => {
        const restored = restore();
        if (!restored.ok) toast.error(restoreProblemText(view.name, restored.problem));
      },
    },
  });
}
