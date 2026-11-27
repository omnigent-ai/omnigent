// With quick_pick_palette on, ⌘K offers the sidebar's selection: Select all and Deselect
// all while selecting, and the selection bar's actions while it shows.

import {
  ArchiveIcon,
  ArchiveRestoreIcon,
  SquareCheckIcon,
  FolderInputIcon,
  type LucideIcon,
  MailIcon,
  MailOpenIcon,
  SquareIcon,
  Trash2Icon,
} from "lucide-react";
import { useMemo, useRef } from "react";

import { useQuickPickPaletteEnabled } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import {
  AVAILABLE,
  HIDDEN,
  type PaletteCommand,
  usePaletteCommands,
} from "@/store/paletteCommands";

import { type BulkSessionCommandId, type BulkSessionFacts, bulkSessionCommands } from "./bulkModel";

const ICONS: Record<BulkSessionCommandId, LucideIcon> = {
  "mark-read": MailOpenIcon,
  "mark-unread": MailIcon,
  archive: ArchiveIcon,
  unarchive: ArchiveRestoreIcon,
  "move-to-project": FolderInputIcon,
  delete: Trash2Icon,
};

const KEYWORDS: Record<BulkSessionCommandId, string[]> = {
  "mark-read": ["selection", "bulk", "seen"],
  "mark-unread": ["selection", "bulk", "unseen"],
  archive: ["selection", "bulk", "hide"],
  unarchive: ["selection", "bulk", "restore"],
  "move-to-project": ["selection", "bulk", "project", "folder"],
  delete: ["selection", "bulk", "remove"],
};

// Run once the palette closes, so the dialog they may open keeps the focus it takes.
const OPENS_DIALOG = new Set<BulkSessionCommandId>(["archive", "delete"]);

const NO_COMMANDS: PaletteCommand[] = [];

// Each selection Set gets its own key, so a row accepted after the selection changed
// names a selection that no longer exists, and the palette does nothing.
const selectionKeys = new WeakMap<ReadonlySet<string>, number>();
let nextSelectionKey = 0;
function selectionKey(selection: ReadonlySet<string>): number {
  let key = selectionKeys.get(selection);
  if (key === undefined) {
    key = ++nextSelectionKey;
    selectionKeys.set(selection, key);
  }
  return key;
}

export interface BulkSessionActions {
  facts: BulkSessionFacts;
  selection: ReadonlySet<string>;
  /** Project names Move offers, as the bar's menu lists them. */
  projects: readonly string[];
  /** The bar's own handlers; delete opens its confirmation. */
  run: Record<Exclude<BulkSessionCommandId, "move-to-project">, () => void>;
  moveToProject: (project: string) => void;
}

/** Offers the selection bar's actions in ⌘K while the bar is mounted. */
export function useBulkSessionPaletteCommands(actions: BulkSessionActions): void {
  const enabled = useQuickPickPaletteEnabled();
  const latest = useRef(actions);
  latest.current = actions;
  const { facts, selection, projects } = actions;
  const key = selectionKey(selection);
  // Rebuilt when a label, availability or project changes, not on every bar render.
  const signature = JSON.stringify([facts, projects]);
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!enabled) return NO_COMMANDS;
    const current = latest.current;
    return bulkSessionCommands(current.facts).map((command) => ({
      id: `sidebar.selection.${command.id}:${key}`,
      label: command.label,
      icon: ICONS[command.id],
      keywords: KEYWORDS[command.id],
      availability: command.availability,
      destructive: command.destructive,
      movesFocus: OPENS_DIALOG.has(command.id),
      ...(command.id === "move-to-project"
        ? {
            options: () =>
              latest.current.projects.map((project) => ({
                id: project,
                label: project,
                run: () => latest.current.moveToProject(project),
              })),
            optionsEmptyMessage: "No projects yet.",
          }
        : {}),
      run: () => {
        if (command.id !== "move-to-project") latest.current.run[command.id]();
      },
    }));
    // `signature` stands in for the facts and projects read through `latest`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, key, signature]);
  usePaletteCommands("sidebar.selection", "Selection", commands);
}

export interface SessionSelection {
  selecting: boolean;
  selected: number;
  selectAll: () => void;
  deselectAll: () => void;
}

/** Offers Select all and Deselect all in ⌘K while the sidebar is selecting sessions. */
export function useSessionSelectionPaletteCommands(selection: SessionSelection): void {
  const enabled = useQuickPickPaletteEnabled();
  const latest = useRef(selection);
  latest.current = selection;
  const { selecting, selected } = selection;
  const commands = useMemo<PaletteCommand[]>(
    () =>
      enabled && selecting
        ? [
            {
              id: "sidebar.selection.select-all",
              label: "Select all sessions",
              icon: SquareCheckIcon,
              keywords: ["selection", "bulk", "every"],
              run: () => latest.current.selectAll(),
            },
            {
              id: "sidebar.selection.deselect-all",
              label: "Deselect all sessions",
              icon: SquareIcon,
              keywords: ["selection", "bulk", "clear", "none"],
              availability: selected > 0 ? AVAILABLE : HIDDEN,
              run: () => latest.current.deselectAll(),
            },
          ]
        : NO_COMMANDS,
    [enabled, selecting, selected],
  );
  usePaletteCommands("sidebar.selection.scope", "Selection", commands);
}
