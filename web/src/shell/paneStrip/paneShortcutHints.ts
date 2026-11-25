// The shortcut a pane's strip and menus show beside a command: the pane key it has here, or none
// while the command has no key.

import { isPaneShortcutId, paneKeyAria, paneKeyCaps } from "../paneShortcuts";

/** The key caps a hint shows for pane command `command`, or undefined without a key. */
export function paneShortcutKeys(command: string | undefined): string[] | undefined {
  if (!isPaneShortcutId(command)) return undefined;
  const keys = paneKeyCaps(command);
  return keys.length > 0 ? keys : undefined;
}

/** The `aria-keyshortcuts` value for pane command `command`, or undefined without a key. */
export function paneShortcutAria(command: string | undefined): string | undefined {
  return isPaneShortcutId(command) ? paneKeyAria(command) : undefined;
}
