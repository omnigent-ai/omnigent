// How a click or key on a pull request row, commit or file asks to open it, as
// browser links do: a plain click or Enter shows it here; ⌘-click (Ctrl-click off
// macOS), a middle click or ⌘Enter open it in a new tab behind the current one;
// adding Shift opens the new tab and switches to it.

import type { KeyboardEvent, MouseEvent } from "react";

import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { isMacPlatform } from "@/lib/hotkeys";
import type { KeyChordInput } from "@/lib/shortcuts/chord";
import { matchesShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

export type PullRequestOpenIntent = "here" | "background" | "foreground";

export interface OpenGesture {
  /** The mouse button; absent for a key. 1 is the middle button. */
  button?: number;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
}

export function pullRequestOpenIntent(
  gesture: OpenGesture,
  mac: boolean = isMacPlatform(),
): PullRequestOpenIntent {
  const newTab = gesture.button === 1 || (mac ? gesture.metaKey : gesture.ctrlKey);
  if (!newTab) return "here";
  return gesture.shiftKey ? "foreground" : "background";
}

/**
 * Whether a key on a focused row asks to open it in a new tab: ⌘Enter, or ⇧⌘Enter to
 * switch to it (Ctrl off macOS), as the registry binds them.
 */
export function isNewTabEnter(
  event: KeyChordInput | globalThis.KeyboardEvent,
  mac: boolean = isMacPlatform(),
): boolean {
  return matchesShortcut(event, "rows.openInNewTab", { mac });
}

/**
 * The props that let an element open what it shows from a click, a middle click or
 * ⌘Enter. `onKeyDown` still runs for every other key, such as a list's arrow keys.
 * The element owns ⌘Enter, so a pending chat prompt's accept chord leaves it be.
 */
export function openGestureProps(
  onOpen: (intent: PullRequestOpenIntent) => void,
  onKeyDown?: (event: KeyboardEvent<HTMLElement>) => void,
) {
  return {
    [OWNS_COMMAND_ENTER_ATTRIBUTE]: "",
    onClick: (event: MouseEvent<HTMLElement>) => onOpen(pullRequestOpenIntent(event)),
    // A middle click opens it behind the current tab, without the browser's autoscroll.
    onMouseDown: (event: MouseEvent<HTMLElement>) => {
      if (event.button === 1) event.preventDefault();
    },
    onAuxClick: (event: MouseEvent<HTMLElement>) => {
      if (event.button !== 1) return;
      event.preventDefault();
      onOpen(pullRequestOpenIntent(event));
    },
    onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
      if (isNewTabEnter(event)) {
        event.preventDefault();
        onOpen(pullRequestOpenIntent(event));
        return;
      }
      onKeyDown?.(event);
    },
  };
}

/**
 * Do what an intent asks: show it here, or in a new tab when the surface has
 * tabs to open. Without them, every gesture shows it here.
 */
export function openByIntent(
  intent: PullRequestOpenIntent,
  here: () => void,
  inNewTab?: (options: { background: boolean }) => void,
): void {
  if (intent === "here" || !inNewTab) here();
  else inNewTab({ background: intent === "background" });
}
