// Move Pane…'s availability for a host's ⌘K, kept current: measured with the panes' size rule as
// the split, its maximized pane or the panes' size change, and as their terminals fit, so a split
// with no room to move a pane lists the command greyed with why, as the drag offers no spot there.

import { type RefObject, useLayoutEffect, useRef, useState } from "react";

import type { TerminalLayout } from "@/lib/paneSplitLayout";
import type { CommandAvailability } from "@/store/paletteCommands";

import { movePaneAvailability, paneMoveLayout } from "./paneMove";
import type { PaneSizing } from "./paneSizing";

export interface PaneMoveAvailabilityOptions {
  layout: TerminalLayout | null | undefined;
  zoomedKey: string | null;
  /** The pane ⌘K would move. */
  paneKey: string | null;
  /** The panes' size rule, measured now. */
  sizing: () => PaneSizing | null;
  /**
   * The element the panes are laid out in, watched for size changes and terminals fitting. Read
   * after each commit, so one that attaches later or is replaced is watched in its turn.
   */
  rootRef: RefObject<HTMLElement | null>;
}

/** A session terminal's fitted grid, which its pane's size rule reads. */
const FIT_ATTRIBUTES = ["data-terminal-cols", "data-terminal-rows"];

const reasonOf = (availability: CommandAvailability | null) =>
  availability && !availability.available ? availability.reason : undefined;
const sameAvailability = (a: CommandAvailability | null, b: CommandAvailability | null) =>
  a === b || (a?.available === b?.available && reasonOf(a) === reasonOf(b));

export function usePaneMoveAvailability({
  layout,
  zoomedKey,
  paneKey,
  sizing,
  rootRef,
}: PaneMoveAvailabilityOptions): CommandAvailability | null {
  const measure = () => movePaneAvailability({ layout, zoomedKey, key: paneKey, sizing: sizing() });
  const latest = useRef(measure);
  latest.current = measure;
  const [measured, setMeasured] = useState(measure);
  const movable = paneMoveLayout(layout, zoomedKey);
  const shape = movable === null ? null : JSON.stringify(movable);
  // What is watched, and for which split: kept in a ref, so reading the ref's element after a
  // commit never renders again by itself.
  const watched = useRef<{ root: HTMLElement | null; key: string; stop: () => void } | null>(null);
  // After every commit, since nothing tells the hook when the ref's element changes.
  useLayoutEffect(() => {
    const root = shape === null ? null : rootRef.current;
    const key = `${shape}\0${paneKey}`;
    if (watched.current && watched.current.root === root && watched.current.key === key) return;
    watched.current?.stop();
    watched.current = { root, key, stop: () => {} };
    if (shape === null) return;
    const update = () =>
      setMeasured((previous) => {
        const next = latest.current();
        return sameAvailability(previous, next) ? previous : next;
      });
    update();
    if (!root) return;
    const resizes = new ResizeObserver(update);
    resizes.observe(root);
    // A restored split's terminals fit after it mounts, in a box that keeps its size: their grids
    // are what session panes measure by, so fitting one measures again.
    const fits = new MutationObserver(update);
    fits.observe(root, { subtree: true, attributeFilter: FIT_ATTRIBUTES });
    watched.current.stop = () => {
      resizes.disconnect();
      fits.disconnect();
    };
  });
  useLayoutEffect(
    () => () => {
      watched.current?.stop();
      watched.current = null;
    },
    [],
  );
  // Not offered at all, as for a lone or maximized pane, at once; room is what's measured.
  if (movable === null) return null;
  return measured;
}
