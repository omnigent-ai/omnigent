// With quick_pick_palette on, the shortcuts that wait for it: each chord runs its ⌘K
// command, so the two share one label, one availability and one handler.

import { useEffect, useRef } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import {
  cancelRecentSessionSwitcher,
  isRecentSessionSwitcherOpen,
} from "@/hooks/useRecentSessionSwitch";
import { getEmbedRoot } from "@/lib/host";
import {
  cancelBrowserRecentSessionSwitch,
  onBrowserRecentSessionInput,
  setBrowserRecentSessionSwitchSupported,
} from "@/lib/nativeBridge";
import { matchesShortcut, type ShortcutId } from "@/lib/shortcuts/registry";
import {
  isCommandAvailable,
  type PaletteCommand,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";

import { useCommandContext } from "../commandContext";
import type { OpenSessionCommandId } from "../openSessionCommands";
import { useSessionNavigationCommands } from "../sessionNavigationCommands";

/** The ⌘K command each chord runs, by its id; the host binds these and no others. */
export const HOSTED_SHORTCUT_COMMANDS = {
  "session.next": "next-session",
  "session.previous": "previous-session",
} as const satisfies Partial<Record<ShortcutId, string>>;

/** The open session's header commands each chord runs, by the command's id for its state. */
export const OPEN_SESSION_SHORTCUT_COMMANDS = {
  "session.pin": ["pin", "unpin"],
  "session.archive": ["archive", "unarchive"],
  "session.fork": ["fork"],
} as const satisfies Partial<Record<ShortcutId, readonly OpenSessionCommandId[]>>;

type HostedShortcutId =
  keyof typeof HOSTED_SHORTCUT_COMMANDS | keyof typeof OPEN_SESSION_SHORTCUT_COMMANDS;

const HOSTED_SHORTCUT_IDS = [
  ...Object.keys(HOSTED_SHORTCUT_COMMANDS),
  ...Object.keys(OPEN_SESSION_SHORTCUT_COMMANDS),
] as HostedShortcutId[];

function registeredCommand(id: string): PaletteCommand | undefined {
  for (const group of Object.values(usePaletteCommandsStore.getState().groups)) {
    const found = group.commands.find((command) => command.id === id);
    if (found) return found;
  }
  return undefined;
}

// Surfaces that own their keystrokes, as for the current session's other chords.
const KEY_OWNING_SURFACES =
  '.xterm, .monaco-editor, [role="dialog"], [role="alertdialog"], [role="menu"]';
// Ctrl+Tab passes over terminals and editors, but a dialog or menu keeps it.
const DIALOG_SURFACES = '[role="dialog"], [role="alertdialog"], [role="menu"]';

const SESSION_STEP_IDS = ["session.next", "session.previous"] as const;

/** Mount once with quick_pick_palette on, beside the palette. */
export function QuickPickShortcutHost() {
  const navigation = useSessionNavigationCommands();
  const openSessionId = useCommandContext().session?.id ?? null;
  const latest = useRef({ navigation, openSessionId });
  latest.current = { navigation, openSessionId };
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    const commandFor = (shortcut: HostedShortcutId): PaletteCommand | undefined => {
      if (shortcut in HOSTED_SHORTCUT_COMMANDS) {
        const commandId =
          HOSTED_SHORTCUT_COMMANDS[shortcut as keyof typeof HOSTED_SHORTCUT_COMMANDS];
        return latest.current.navigation.find((command) => command.id === commandId);
      }
      // The open session's header registers the row for its current state, such as Unpin.
      const { openSessionId: sessionId } = latest.current;
      if (sessionId === null) return undefined;
      return OPEN_SESSION_SHORTCUT_COMMANDS[shortcut as keyof typeof OPEN_SESSION_SHORTCUT_COMMANDS]
        .map((command) => registeredCommand(`open-session:${sessionId}:${command}`))
        .find((command) => command !== undefined);
    };
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.defaultPrevented || event.isComposing) return;
      const shortcut = HOSTED_SHORTCUT_IDS.find((id) => matchesShortcut(event, id));
      if (!shortcut) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      // Ctrl+Tab steps from a terminal or editor too, as the recent switcher it replaces did.
      const tabStep = event.key === "Tab";
      const focused = document.activeElement;
      const owningSurfaces = tabStep ? DIALOG_SURFACES : KEY_OWNING_SURFACES;
      // Over a held recent-session switcher, Ctrl+Tab drops the recency switch and steps.
      const overRecentSwitcher = tabStep && isRecentSessionSwitcherOpen();
      if (!overRecentSwitcher && focused instanceof Element && focused.closest(owningSurfaces)) {
        return;
      }
      const command = commandFor(shortcut);
      if (!command || !isCommandAvailable(command.availability)) return;
      // A held Ctrl+Tab steps once; its repeats are consumed so a terminal never sees Tabs.
      if (event.repeat && !tabStep) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.repeat) return;
      if (overRecentSwitcher) cancelRecentSessionSwitcher();
      command.run();
    };
    // An embedded Browser page is its own WebContents, so the shell forwards its Ctrl+Tab.
    const unsubscribeBrowserInput = onBrowserRecentSessionInput((input) => {
      if (input.type !== "keydown" || input.key !== "Tab") return;
      const shortcut = SESSION_STEP_IDS.find((id) => matchesShortcut(input, id));
      if (!shortcut) return;
      // Older shells latch for a release-to-switch; clear it so the page keeps its Escape.
      void cancelBrowserRecentSessionSwitch();
      const command = commandFor(shortcut);
      if (!input.repeat && command && isCommandAvailable(command.availability)) command.run();
    });
    void setBrowserRecentSessionSwitchSupported(true);
    // Capture, so a chord that another page listener also reads, such as the transcript's
    // message steps with Shift held, is this host's alone.
    window.addEventListener("keydown", handler, true);
    return () => {
      window.removeEventListener("keydown", handler, true);
      unsubscribeBrowserInput();
      void setBrowserRecentSessionSwitchSupported(false);
    };
  }, [isEmbedded]);
  return null;
}
