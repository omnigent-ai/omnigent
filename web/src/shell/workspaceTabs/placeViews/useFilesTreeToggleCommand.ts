// Showing or hiding the Files tree: one action for ⇧⌘E, its ⌘K row and the toolbar's
// toggle, offered by the Files place on screen. A wide page docks the tree; a narrow
// one opens it as a popover under the toggle.

import { ListTreeIcon } from "lucide-react";
import { useEffect, useRef } from "react";

import { hotkeyAvailability, useHotkeyCommand } from "@/hooks/useHotkeyCommand";
import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

import type { WorkspaceSidebarState } from "../WorkspaceSidebarLayout";

export const SHOW_FILES_TREE_LABEL = "Show Files Tree";
export const HIDE_FILES_TREE_LABEL = "Hide Files Tree";

/** The toggle's wording for a tree that is or isn't on screen. */
export function filesTreeToggleLabel(shown: boolean): string {
  return shown ? HIDE_FILES_TREE_LABEL : SHOW_FILES_TREE_LABEL;
}

/** Registers the ⌘K row and binds the chord while `active`; both run `tree.toggle`. */
export function useFilesTreeToggleCommand(
  tree: WorkspaceSidebarState,
  active: boolean,
  isMac = isMacPlatform(),
): void {
  const toggle = useRef(tree.toggle);
  toggle.current = tree.toggle;
  useHotkeyCommand("files.toggleTree", {
    icon: ListTreeIcon,
    label: filesTreeToggleLabel(tree.shown),
    keywords: ["files", "tree", "explorer", "sidebar", "folders"],
    run: () => toggle.current(),
    availability: hotkeyAvailability(active),
    // The popover takes focus, so it opens once the palette has given focus back.
    movesFocus: true,
  });

  useEffect(() => {
    if (!active) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (!matchesShortcut(event, "files.toggleTree", { mac: isMac })) return;
      event.preventDefault();
      event.stopPropagation();
      // Holding the chord would flap the tree in and out.
      if (event.repeat) return;
      toggle.current();
    };
    // Captured, so an editor that owns its keystrokes doesn't swallow the chord.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [active, isMac]);
}
