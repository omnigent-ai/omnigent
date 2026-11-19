// Previous / Next Sidebar Tab (⇧⌘[ / ⇧⌘]) in the workspace sidebar, which the page handles
// except in a code editor, where those keys fold and unfold.
import { useEffect, useRef } from "react";

import { TAB_KEY_OWNING_DIALOG_OR_MENU } from "@/lib/hotkeys";
import { matchesShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

/** Monaco and the CodeMirror file editor both bind the fold keys. */
const FOLDING_EDITORS = ".monaco-editor, .cm-editor";

/**
 * The entry one step from `current` in `direction`, wrapping at both ends. With nothing
 * selected, the first entry going forward and the last going back.
 */
export function adjacentStripEntry<Id>(
  entryIds: readonly Id[],
  current: Id | null,
  direction: -1 | 1,
): Id | null {
  if (entryIds.length === 0) return null;
  const index = current === null ? -1 : entryIds.indexOf(current);
  if (index === -1) return entryIds[direction === 1 ? 0 : entryIds.length - 1]!;
  return entryIds[(index + direction + entryIds.length) % entryIds.length]!;
}

/** Run `onStep` for Previous / Next Sidebar Tab, except in a code editor, which folds on them. */
export function useWorkspaceTabStepChords(onStep: ((direction: -1 | 1) => void) | null): void {
  const latest = useRef(onStep);
  latest.current = onStep;
  const enabled = onStep !== null;
  useEffect(() => {
    if (!enabled) return;
    const handler = (event: KeyboardEvent) => {
      const direction = matchesShortcut(event, "workspace.previousTab")
        ? -1
        : matchesShortcut(event, "workspace.nextTab")
          ? 1
          : 0;
      if (direction === 0 || event.repeat) return;
      const active = document.activeElement;
      if (active?.closest(FOLDING_EDITORS) || active?.closest(TAB_KEY_OWNING_DIALOG_OR_MENU)) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      latest.current?.(direction);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}

/** The strip's button for `tabId`, which carries it as `data-workspace-tab-id`. */
function stripTabButton(tabId: string): HTMLElement | undefined {
  return [...document.querySelectorAll<HTMLElement>("[data-workspace-tab-id]")].find(
    (element) => element.dataset.workspaceTabId === tabId,
  );
}

/** The tabs sidebar: step its strip's entries in order, wrapping, focusing the tab. */
export function useWorkspaceTabStepping({
  entryIds,
  selectedId,
  onSelect,
  enabled,
}: {
  /** The strip's entries in order. */
  entryIds: readonly string[];
  /** The entry the strip shows as selected, if any. */
  selectedId: string | null;
  onSelect: (tabId: string) => void;
  enabled: boolean;
}): void {
  const select = (next: string | null) => {
    if (next === null) return;
    onSelect(next);
    // Focus follows the selection once the strip has rendered it.
    requestAnimationFrame(() => stripTabButton(next)?.focus());
  };
  const step = (direction: -1 | 1) => select(adjacentStripEntry(entryIds, selectedId, direction));
  const active = enabled && entryIds.length > 0;
  useWorkspaceTabStepChords(active ? step : null);
}
