// New, Close and Reopen workspace tab: one action each for the chords and their
// ⌘K rows. Bound while the tab design is mounted, including with the sidebar
// closed, where New tab opens it and the others say why they wait.

import { useEffect, useMemo, useRef } from "react";
import { toast } from "sonner";

import { isMacPlatform } from "@/lib/hotkeys";
import type { Chord } from "@/lib/shortcuts/chord";
import {
  activeShortcutChords,
  matchesShortcut,
} from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import type { WorkspaceTabActionCommand, WorkspaceTabListFacts } from "./model";
import { workspaceCommandIcon } from "./useWorkspacePaletteCommands";

export interface WorkspaceTabChordOptions {
  /** The routed session, which scopes the ⌘K rows; null offers nothing. */
  conversationId: string | null;
  /** The tab actions; only those with a chord are bound here. */
  commands: readonly WorkspaceTabActionCommand[];
  /** What the tab list looks like now, which says why a tab can't close. */
  facts: WorkspaceTabListFacts;
  /** Run an action; only called when it is available. */
  run: (command: WorkspaceTabActionCommand, origin?: WorkspaceTabRunOrigin) => void;
}

/** Where a run came from, which a close hands on to its focus. */
export interface WorkspaceTabRunOrigin {
  /** Focus was in the sidebar, so it stays there on the tab selected next. */
  fromWorkspaceSidebar: boolean;
}

/** Registers the chorded tab actions' ⌘K rows and binds their chords. */
export function useWorkspaceTabChords(
  { conversationId, commands, facts, run }: WorkspaceTabChordOptions,
  isMac = isMacPlatform(),
): void {
  const runRef = useRef(run);
  runRef.current = run;
  const factsRef = useRef(facts);
  factsRef.current = facts;
  const chorded = useMemo(
    () => (conversationId === null ? [] : commands.filter((command) => command.shortcut)),
    [conversationId, commands],
  );
  const chordedRef = useRef(chorded);
  chordedRef.current = chorded;
  // Rebuilt only when what the rows say changes, not on every render of the panel.
  const signature = JSON.stringify(
    chorded.map((command) => [command.id, command.label, command.availability]),
  );
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      chordedRef.current.map((command) => ({
        // The session is part of the id, so a row built for another session never runs.
        id: `workspace:${conversationId}:${command.id}`,
        label: command.label,
        icon: workspaceCommandIcon(command),
        keywords: [...command.keywords],
        availability: command.availability,
        // The sidebar's own chords aren't the app's, so they show as key caps only.
        nativeShortcut: command.shortcut
          ? (activeShortcutChords(command.shortcut, { mac: isMac })[0] as Chord | undefined)
          : undefined,
        // A close can open the unsaved-changes dialog, so it runs once the palette closed.
        movesFocus: true,
        run: () => runRef.current(command),
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [signature, conversationId, isMac],
  );
  usePaletteCommands(`workspace-tabs:${conversationId ?? "none"}`, "Workspace", paletteCommands);

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent): void => {
      const command = chordedRef.current.find(
        (candidate) =>
          candidate.shortcut !== undefined &&
          matchesShortcut(event, candidate.shortcut, { mac: isMac }),
      );
      if (!command) return;
      // The chord is the workspace's even when the action is unavailable.
      event.preventDefault();
      event.stopPropagation();
      if (event.repeat) return;
      if (command.availability.available) runRef.current(command);
      else if (command.action === "close-tab" && factsRef.current.selectedCloseRefusal) {
        toast.info(factsRef.current.selectedCloseRefusal);
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isMac]);
}
