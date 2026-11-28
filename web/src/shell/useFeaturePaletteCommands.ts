// The commands mounted features offer ⌘K (see `featurePaletteRows`), for either palette:
// their rows, and running the one accepted. They load only where the sidebar shows as tabs.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type { FeaturePaletteRow, FeaturePaletteSource } from "./featurePaletteRows";
import { useTabsSidebar } from "./useTabsSidebar";

export type { FeaturePaletteRow };

let loading: Promise<FeaturePaletteSource> | null = null;
let loaded: FeaturePaletteSource | null = null;

/** The rows' source; a failed load is forgotten, so the next mount tries again. */
function loadSource(): Promise<FeaturePaletteSource> {
  loading ??= import("./featurePaletteRows").then(
    (module) => {
      loaded = module.featurePaletteSource;
      return loaded;
    },
    (error: unknown) => {
      loading = null;
      throw error;
    },
  );
  return loading;
}

const NO_ROWS: readonly FeaturePaletteRow[] = [];
const noSubscription = () => () => {};

export interface FeaturePaletteCommands {
  /** The rows to list while the palette is open. */
  rows: readonly FeaturePaletteRow[];
  /**
   * Run the row's command as it's offered now; one withdrawn or unavailable since does
   * nothing. `close` closes the palette unless the command keeps it open.
   */
  accept: (row: FeaturePaletteRow, close: () => void) => void;
  /** The palette dialog's `onCloseAutoFocus`: a command that moves focus runs once it's back. */
  onCloseAutoFocus: () => void;
}

export function useFeaturePaletteCommands(open: boolean): FeaturePaletteCommands {
  // Where the sidebar shows as tabs, as the app shell decides; elsewhere nothing loads.
  const enabled = useTabsSidebar();
  const [source, setSource] = useState(loaded);
  useEffect(() => {
    if (!enabled || source !== null) return;
    // Without its rows the palette still lists everything else.
    loadSource().then(
      (value) => setSource(value),
      (error: unknown) => console.warn("[palette] workspace commands failed to load:", error),
    );
  }, [enabled, source]);
  const listed = enabled && open ? source : null;
  const rows = useSyncExternalStore(
    listed?.subscribe ?? noSubscription,
    () => listed?.rows() ?? NO_ROWS,
  );

  const runAfterClose = useRef<(() => void) | null>(null);
  // Reopened before the last close handed focus back, the user moved on: that close's command
  // never runs, rather than on some later, unrelated close.
  useEffect(() => {
    if (open) runAfterClose.current = null;
  }, [open]);
  const accept = useCallback(
    (row: FeaturePaletteRow, close: () => void) => {
      const command = source?.command(row.id) ?? null;
      if (command === null) {
        close();
        return;
      }
      if (command.keepsPaletteOpen) {
        command.run();
        return;
      }
      if (command.movesFocus) runAfterClose.current = command.run;
      close();
      if (!command.movesFocus) command.run();
    },
    [source],
  );
  const onCloseAutoFocus = useCallback(() => {
    const run = runAfterClose.current;
    runAfterClose.current = null;
    // The dialog returns focus right after this handler, so the command runs after that.
    if (run) queueMicrotask(run);
  }, []);
  return useMemo(() => ({ rows, accept, onCloseAutoFocus }), [rows, accept, onCloseAutoFocus]);
}
