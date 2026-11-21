// Back and Forward through the workspace history: one action each for the chevrons, the
// chords and their ⌘K rows. The chords work while the sidebar is closed, which opens first.

import { useEffect, useMemo, useRef } from "react";

import {
  isFocusInDialogOrMenu,
  isKeyOwningOverlayOpen,
  isMacPlatform,
  isTextEntry,
} from "@/lib/hotkeys";
import type { Chord } from "@/lib/shortcuts/chord";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import {
  activeShortcutChords,
  matchesShortcut,
} from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

import {
  type WorkspaceHistoryCommand,
  type WorkspaceHistoryDirection,
  type WorkspaceHistoryFacts,
  workspaceHistoryCommands,
} from "./model";
import { workspaceCommandIcon } from "./useWorkspacePaletteCommands";

export interface WorkspaceHistoryCommandOptions {
  /** The session whose history this is, which scopes the ⌘K rows; null offers nothing. */
  conversationId: string | null;
  /** Where each step lands; null when there's no history to step. */
  facts: WorkspaceHistoryFacts | null;
  /** Step, opening a closed sidebar first. Only called when the step is available. */
  go: (direction: WorkspaceHistoryDirection) => void;
}

const SHORTCUTS = { back: "workspace.back", forward: "workspace.forward" } as const;

/** Editors and terminals whose own keys include ⌘← and ⌘→. */
const LINE_KEY_SURFACES = ".cm-editor, .monaco-editor, .xterm";

/** Whether focus moves its caret with a plain ⌘← / ⌘→, the macOS app's history chords. */
export function focusOwnsLineKeys(event: Pick<KeyboardEvent, "altKey" | "shiftKey">): boolean {
  if (event.altKey || event.shiftKey) return false;
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return false;
  return isTextEntry(active) || active.closest(LINE_KEY_SURFACES) !== null;
}

/** Registers the ⌘K rows and binds the chords; returns the commands for the chevrons. */
export function useWorkspaceHistoryCommands(
  { conversationId, facts, go }: WorkspaceHistoryCommandOptions,
  isMac = isMacPlatform(),
): WorkspaceHistoryCommand[] {
  const goRef = useRef(go);
  goRef.current = go;
  const commands = useMemo(
    () => (conversationId !== null && facts ? workspaceHistoryCommands(facts) : []),
    [conversationId, facts],
  );
  const commandsRef = useRef(commands);
  commandsRef.current = commands;

  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      commands.map((command) => ({
        // The session is part of the id, so a row built for another session never runs.
        id: `workspace:${conversationId}:${command.id}`,
        label: command.label,
        icon: workspaceCommandIcon(command),
        keywords: [...command.keywords],
        availability: command.availability,
        // The sidebar's own chords aren't the app's, so they show as key caps only.
        nativeShortcut: activeShortcutChords(SHORTCUTS[command.direction], { mac: isMac })[0] as
          Chord | undefined,
        // A step can focus the tab it lands on or open the unsaved-changes dialog,
        // so it runs once the palette has closed and won't take focus back.
        movesFocus: true,
        run: () => goRef.current(command.direction),
      })),
    [commands, conversationId, isMac],
  );
  usePaletteCommands(`workspace-history:${conversationId ?? "none"}`, "Workspace", paletteCommands);

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (isFocusInDialogOrMenu() || isKeyOwningOverlayOpen() || focusOwnsLineKeys(event)) return;
      const command = commandsRef.current.find((candidate) =>
        matchesShortcut(event, SHORTCUTS[candidate.direction], { mac: isMac }),
      );
      if (!command) return;
      // The chord is the workspace's even when the step is unavailable, so nothing else acts on it.
      event.preventDefault();
      event.stopPropagation();
      // A held chord would queue one guarded navigation per repeat.
      if (event.repeat || !command.availability.available) return;
      goRef.current(command.direction);
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isMac]);

  return commands;
}
