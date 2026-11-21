// Show Pane Toolbar: the keyboard moves into a pane's toolbar, its strip row or the chat pane's
// header actions, where the arrows move between its buttons and Escape gives it back to the pane.

import type { KeyboardEvent as ReactKeyboardEvent } from "react";

import { showToast } from "@/components/ui/toast";

/** Marks the toolbar that holds pane `value`'s actions. */
export const PANE_TOOLBAR_ATTRIBUTE = "data-pane-toolbar";
/** Marks one of a pane toolbar's buttons, which the keyboard moves between. */
export const PANE_TOOLBAR_BUTTON_ATTRIBUTE = "data-pane-toolbar-button";

// Buttons hide for a narrow pane; every pane's actions are in the palette.
export const PANE_TOOLBAR_HIDDEN_MESSAGE =
  "This pane's toolbar buttons are hidden. Run its actions from the command palette.";

/** Gives the first button of `paneKey`'s toolbar under `root` the keyboard; false without one. */
export function focusPaneToolbar(root: ParentNode | null, paneKey: string): boolean {
  const toolbar = root?.querySelector<HTMLElement>(
    `[${PANE_TOOLBAR_ATTRIBUTE}="${CSS.escape(paneKey)}"]`,
  );
  const button = toolbar?.querySelector<HTMLButtonElement>(
    `[${PANE_TOOLBAR_BUTTON_ATTRIBUTE}]:not([disabled])`,
  );
  if (!button) return false;
  button.focus();
  return true;
}

/** ⌘K's Show Pane Toolbar on `paneKey`: its first button, or where its actions are instead. */
export function showPaneToolbar(root: ParentNode | null, paneKey: string): void {
  if (!focusPaneToolbar(root, paneKey)) showToast(PANE_TOOLBAR_HIDDEN_MESSAGE);
}

/** Escape on a toolbar button gives the keyboard back to its pane. */
export function returnFromPaneToolbar(
  event: ReactKeyboardEvent<HTMLElement>,
  focusPane: (() => void) | undefined,
): void {
  if (event.key !== "Escape" || !focusPane) return;
  if (!(event.target as Element).closest(`[${PANE_TOOLBAR_BUTTON_ATTRIBUTE}]`)) return;
  event.preventDefault();
  event.stopPropagation();
  focusPane();
}

/**
 * When a toolbar button pressed from the keyboard gives its pane the keyboard: before the command,
 * so a split or close acts as from inside the pane, or after it, once Maximize or Restore has laid
 * the panes out. A click leaves the keyboard where it was, and runs the command alone.
 */
export type PaneToolbarHandoff = "before" | "after" | "none";

/** Runs a toolbar button's `command`, handing its pane the keyboard as `handoff` says. */
export function runFromPaneToolbar(
  button: HTMLElement,
  command: () => void,
  focusPane: (() => void) | undefined,
  handoff: PaneToolbarHandoff,
): void {
  const fromKeyboard = button.ownerDocument.activeElement === button;
  if (fromKeyboard && handoff === "before") focusPane?.();
  command();
  if (fromKeyboard && handoff === "after") focusPane?.();
}
