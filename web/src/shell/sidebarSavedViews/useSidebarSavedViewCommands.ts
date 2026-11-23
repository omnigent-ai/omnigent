// ⌘K's rows for the sidebar's saved views: apply one, save the sidebar as it is, or delete one.

import { BookmarkIcon, BookmarkPlusIcon, Trash2Icon } from "lucide-react";
import { useMemo, useRef } from "react";

import type { SavedViewScope, SidebarSavedView } from "@/lib/sidebarSavedViews";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import { deleteViewWithUndo } from "./savedViewActions";

export interface SidebarSavedViewCommandsProps {
  /** Whose views; null offers no rows. */
  scope: SavedViewScope | null;
  views: readonly SidebarSavedView[];
  /** Shows the sidebar as `view` says. */
  apply: (view: SidebarSavedView) => void;
  /** Opens the dialog that names the sidebar as it is. */
  openSave: () => void;
}

export function useSidebarSavedViewCommands({
  scope,
  views,
  apply,
  openSave,
}: SidebarSavedViewCommandsProps): void {
  const latest = useRef({ scope, apply, openSave });
  latest.current = { scope, apply, openSave };
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!scope) return [];
    // The scope is part of each id, so a row built for another server or user never runs, and a
    // row run after the palette closed does nothing once the scope has changed meanwhile.
    const prefix = `sidebar-view:${scope.server}:${scope.userId}`;
    const inScope = (run: () => void) => () => {
      const now = latest.current.scope;
      if (now?.server === scope.server && now.userId === scope.userId) run();
    };
    return [
      ...views.map((view): PaletteCommand => ({
        id: `${prefix}:apply:${view.id}`,
        label: `Sidebar view: ${view.name}`,
        icon: BookmarkIcon,
        keywords: ["saved view", "filter", "sessions", view.name],
        leadsSources: false,
        run: inScope(() => latest.current.apply(view)),
      })),
      {
        id: `${prefix}:save`,
        label: "Save sidebar view…",
        icon: BookmarkPlusIcon,
        keywords: ["saved view", "filter", "sessions", "new view"],
        movesFocus: true,
        run: inScope(() => latest.current.openSave()),
      },
      ...views.map((view): PaletteCommand => ({
        id: `${prefix}:delete:${view.id}`,
        label: `Delete sidebar view: ${view.name}`,
        icon: Trash2Icon,
        keywords: ["saved view", "remove", view.name],
        destructive: true,
        leadsSources: false,
        run: inScope(() => deleteViewWithUndo(scope, view)),
      })),
    ];
  }, [scope, views]);
  usePaletteCommands("sidebar-saved-views", "Sidebar views", commands);
}
