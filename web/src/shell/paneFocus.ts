// Where the pane keys and ⌘K learn which pane of a split has the keyboard, and how they move
// it. Session panes are terminals in this page, so their focus is the page's own DOM focus.

import type { RefObject } from "react";

import { isFocusInDialogOrMenu, isTextEntry } from "@/lib/hotkeys";
import {
  CHAT_PANE_BODY,
  CHAT_PANE_BODY_ATTRIBUTE,
  TERMINAL_PANE,
  TERMINAL_SURFACE,
} from "@/lib/paneSurfaces";
import { domCellSizing, type PaneSizing } from "./paneSizing";
import { focusSplitPane } from "./splitTargetActions";
import type { SplitTarget } from "./terminalSplitTarget";

/** What happens to focus in a split's panes, as the pane keys follow it. */
export interface PaneFocusListener {
  /** Focus left the panes from ``where``. */
  left: (where: unknown) => void;
  /** Focus came into the panes at ``where``. */
  entered: (where: unknown) => void;
  /** The person clicked or typed in the panes. */
  acted: () => void;
}

export interface PaneFocus {
  /** The keyboard is in one of the panes' terminals. */
  hasFocus: () => boolean;
  /** The pane whose terminal has the keyboard, or null. */
  focusedPane: () => string | null;
  /** Make ``key`` the split's focused pane. */
  focus: (key: string) => void;
  /** The panes' area, width over height; 1 while it has no size. */
  aspect: () => number;
  /** The size rule for user actions over the panes as they are now; null until measured. */
  sizing: () => PaneSizing | null;
  subscribe: (listener: PaneFocusListener) => () => void;
}

/** Whether the keyboard is in session ``conversationId``'s web chat, not another session's. */
export function focusInChatPane(conversationId: string): boolean {
  const body = document.activeElement?.closest(CHAT_PANE_BODY);
  return body?.getAttribute(CHAT_PANE_BODY_ATTRIBUTE) === conversationId;
}

/** Whether the keyboard is on the page itself, not in a field, terminal, dialog or menu. */
export function focusOnPage(): boolean {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || active === document.body) return true;
  return !isTextEntry(active) && !active.closest(TERMINAL_SURFACE) && !isFocusInDialogOrMenu();
}

/** The pane under focus inside ``root``, wherever in the pane focus is. */
export function paneUnderFocus(root: HTMLElement | null): string | null {
  const active = document.activeElement;
  if (!root || !active || !root.contains(active)) return null;
  return active.closest<HTMLElement>(TERMINAL_PANE)?.dataset.terminalPane ?? null;
}

/** The pane under focus inside ``root``, when focus is in its terminal surface. */
export function focusedTerminalPaneKey(root: HTMLElement | null): string | null {
  return document.activeElement?.closest(TERMINAL_SURFACE) ? paneUnderFocus(root) : null;
}

/** `element`'s width over its height, as pane directions measure the view; 1 before it has a size. */
export function elementAspect(element: Element | null | undefined): number {
  const rect = element?.getBoundingClientRect();
  return rect && rect.height > 0 ? rect.width / rect.height : 1;
}

/** Focus in panes rendered in this page under ``panesRef``, as the session's terminals are. */
export function domPaneFocus(
  target: SplitTarget,
  panesRef: RefObject<HTMLElement | null>,
): PaneFocus {
  const within = (node: unknown): boolean =>
    node instanceof Node && !!panesRef.current?.contains(node);
  return {
    hasFocus: () => within(document.activeElement),
    focusedPane: () => focusedTerminalPaneKey(panesRef.current),
    focus: (key) => focusSplitPane(target, key),
    aspect: () => elementAspect(panesRef.current),
    sizing: () => domCellSizing(panesRef.current),
    subscribe: ({ left, entered, acted }) => {
      const onFocusOut = (event: FocusEvent) => {
        if (!within(event.target) || within(event.relatedTarget)) return;
        left(event.target);
      };
      const onFocusIn = (event: FocusEvent) => {
        if (!within(event.target) || within(event.relatedTarget)) return;
        entered(event.target);
      };
      const onAct = (event: Event) => {
        if (within(event.target)) acted();
      };
      document.addEventListener("focusout", onFocusOut, true);
      document.addEventListener("focusin", onFocusIn, true);
      document.addEventListener("pointerdown", onAct, true);
      document.addEventListener("keydown", onAct, true);
      return () => {
        document.removeEventListener("focusout", onFocusOut, true);
        document.removeEventListener("focusin", onFocusIn, true);
        document.removeEventListener("pointerdown", onAct, true);
        document.removeEventListener("keydown", onAct, true);
      };
    },
  };
}

// What a click in the chat keeps for itself: its controls, links, editable text and dialogs.
const CHAT_CLICK_KEEPS =
  'a, button, input, textarea, select, summary, label, [contenteditable]:not([contenteditable="false"]), [role="button"], [role="link"], [role="menuitem"], [role="tab"], [role="dialog"], [tabindex]:not([tabindex="-1"])';

/**
 * Whether a click on `target` in the chat pane gives the composer the keyboard: a click on the
 * chat's own space, not on a control, and not the end of a text selection, which it would clear.
 */
export function chatClickTakesKeyboard(target: Element, selection: Selection | null): boolean {
  if (selection && !selection.isCollapsed && selection.toString().length > 0) return false;
  return target.closest(CHAT_CLICK_KEEPS) === null;
}
