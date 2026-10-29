// Native menu commands from the desktop shell: File → Close (⌘W), New Terminal
// (⌃`), New Browser Tab (⌘T), and Session → Pin (⌥⌘P) / Archive (⇧⌘A). The
// shell's menu owns those accelerators so they also fire while an embedded
// browser view has focus, which the page's own key listeners never see. The
// shell enables each menu item only while some mounted surface handles it here;
// see web/electron/src/desktopCommands.js for the shell side.

import { useEffect, useRef } from "react";

import { isFocusInDialogOrMenu } from "@/lib/hotkeys";
import {
  type DesktopCommand,
  type DesktopCommandEvent,
  onDesktopCommand,
  setDesktopCommandState,
  supportsDesktopCommands,
} from "@/lib/nativeBridge";

export type { DesktopCommand, DesktopCommandEvent };

/** Return false to pass the command to the next (lower-priority) handler. */
export type DesktopCommandHandler = (event: DesktopCommandEvent) => boolean | void;

interface Registration {
  command: DesktopCommand;
  priority: number;
  /** Pinned / archived state of the open session, for the menu's labels. */
  active: boolean | undefined;
  handle: DesktopCommandHandler;
}

const registrations = new Set<Registration>();
let unsubscribeBridge: (() => void) | null = null;
let reportQueued = false;
let lastReported: string | null = null;

function dispatch(event: DesktopCommandEvent): void {
  // A modal dialog or open menu owns the keyboard, like a sheet in a native app.
  // Quit still asks, so ⌘Q never goes dead behind a dialog.
  if (event.command !== "quit" && isFocusInDialogOrMenu()) return;
  const candidates = [...registrations]
    .filter((registration) => registration.command === event.command)
    .sort((a, b) => b.priority - a.priority);
  for (const registration of candidates) {
    if (registration.handle(event) !== false) return;
  }
}

function isActive(command: DesktopCommand): boolean {
  return [...registrations].some(
    (registration) => registration.command === command && registration.active === true,
  );
}

function reportState(): void {
  reportQueued = false;
  const commands = [...new Set([...registrations].map((registration) => registration.command))];
  const state = {
    commands: commands.sort(),
    sessionPinned: isActive("toggle-pin-session"),
    sessionArchived: isActive("archive-session"),
  };
  const serialized = JSON.stringify(state);
  if (serialized === lastReported) return;
  lastReported = serialized;
  setDesktopCommandState(state);
}

// Coalesce the unregister/register pairs a re-render produces into one report.
function queueReport(): void {
  if (reportQueued) return;
  reportQueued = true;
  queueMicrotask(reportState);
}

function register(registration: Registration): () => void {
  registrations.add(registration);
  unsubscribeBridge ??= onDesktopCommand(dispatch);
  queueReport();
  return () => {
    registrations.delete(registration);
    queueReport();
  };
}

/**
 * Handle a native menu command while mounted. A null handler unregisters it,
 * which disables the shell's menu item. Handlers run highest `priority` first;
 * `active` reports the open session's pinned/archived state for the menu label.
 */
export function useDesktopCommand(
  command: DesktopCommand,
  handler: DesktopCommandHandler | null,
  { priority = 0, active }: { priority?: number; active?: boolean } = {},
): void {
  // Held in a ref so a fresh closure each render doesn't re-register.
  const latest = useRef(handler);
  latest.current = handler;
  const enabled = handler !== null && supportsDesktopCommands();

  useEffect(() => {
    if (!enabled) return;
    return register({
      command,
      priority,
      active,
      handle: (event) => (latest.current ? latest.current(event) : false),
    });
  }, [enabled, command, priority, active]);
}

/** Forget registrations, the bridge subscription, and the last report. */
export function resetDesktopCommandsForTests(): void {
  registrations.clear();
  unsubscribeBridge?.();
  unsubscribeBridge = null;
  reportQueued = false;
  lastReported = null;
}
