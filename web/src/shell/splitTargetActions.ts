// What a split's layout commands do to its target, shared by the pane keys, ⌘K, menus and
// drags so they act on the same pane with the same rules. Starting and ending shells is the
// pane actions hook's; these only change the layout.

import {
  closeTerminalPane,
  equalizeTerminalLayout,
  neighborTerminalPane,
  nudgeTerminalSplit,
  splitTerminalLayout,
  terminalLayoutKeys,
  type TerminalLayout,
  type TerminalPaneDirection,
  type TerminalPanePlacement,
  type TerminalSplitAxis,
} from "@/lib/paneSplitLayout";

import type { PaneSizing } from "./paneSizing";
import type { SplitTarget, TerminalSplitTarget } from "./terminalSplitTarget";

/**
 * Put ``newKey`` beside ``sourceKey``, the pane the split was asked from. Nothing changes once
 * that pane is gone or ``newKey`` is already shown; callers check room with {@link roomToSplit}.
 */
export function insertSplitPane(
  target: SplitTarget,
  {
    sourceKey,
    newKey,
    axis,
    placement = "after",
    unsplitKey,
    takeFocus,
  }: {
    sourceKey: string;
    newKey: string;
    axis: TerminalSplitAxis;
    /** Whether the new pane goes right of/below the source or left of/above it. */
    placement?: TerminalPanePlacement;
    /** The pane the view shows while unsplit: the agent's terminal. */
    unsplitKey: string | null;
    /** Whether the new pane takes focus; otherwise the source stays the focused pane. */
    takeFocus: boolean;
  },
): boolean {
  let inserted = false;
  target.update((current) => {
    const state =
      current ?? (unsplitKey === null ? null : { layout: unsplitKey, focusedKey: unsplitKey });
    if (state === null) return current;
    const keys = terminalLayoutKeys(state.layout);
    if (!keys.includes(sourceKey) || keys.includes(newKey)) return current;
    inserted = true;
    return {
      layout: splitTerminalLayout(state.layout, sourceKey, newKey, axis, placement),
      focusedKey: takeFocus && state.focusedKey === sourceKey ? newKey : state.focusedKey,
    };
  });
  return inserted;
}

export const PANE_CLOSED_MESSAGE = "That terminal pane has already closed.";
export const SPLIT_CLOSING_MESSAGE = "Wait for the split to finish closing.";

// Each split's host measures its panes; every entry point to that split checks against it.
const splitSizings = new Map<string, () => PaneSizing | null>();

/** Register how ``targetId``'s host measures its panes now; returns the unregister. */
export function registerSplitSizing(
  targetId: string,
  measure: () => PaneSizing | null,
): () => void {
  splitSizings.set(targetId, measure);
  return () => {
    if (splitSizings.get(targetId) === measure) splitSizings.delete(targetId);
  };
}

/** ``target``'s size rule as its host measures its panes now; null when unmeasured. */
export function measureSplitSizing(target: SplitTarget): PaneSizing | null {
  return splitSizings.get(target.id)?.() ?? null;
}

/** Whether splitting ``sourceKey`` of ``target`` now leaves every pane at least the minimum. */
export function roomToSplit(
  target: TerminalSplitTarget,
  sourceKey: string,
  axis: TerminalSplitAxis,
  placement: TerminalPanePlacement,
  unsplit: string | null,
): boolean {
  const sizing = measureSplitSizing(target);
  const current = target.read()?.layout ?? unsplit;
  if (sizing === null || current === null) return true;
  return sizing.fits(splitTerminalLayout(current, sourceKey, "\u0000new", axis, placement));
}

/** Whether ``after`` may replace ``before``; any change goes while nothing is measured. */
export function keepsRoom(
  before: TerminalLayout,
  after: TerminalLayout,
  sizing: PaneSizing | null,
): boolean {
  return sizing === null || sizing.keeps(before, after);
}

export function focusSplitPane(target: SplitTarget, key: string): void {
  target.update((current) =>
    current && current.focusedKey !== key && terminalLayoutKeys(current.layout).includes(key)
      ? { ...current, focusedKey: key }
      : current,
  );
}

/** Focus the pane beside ``fromKey``; ``aspect`` is the view's width over its height. */
export function moveSplitFocus(
  target: SplitTarget,
  fromKey: string,
  direction: TerminalPaneDirection,
  aspect: number,
): string | null {
  const current = target.read();
  if (!current) return null;
  const neighbor = neighborTerminalPane(current.layout, fromKey, direction, aspect);
  if (neighbor !== null) focusSplitPane(target, neighbor);
  return neighbor;
}

/** Nudge ``key``'s nearest divider; ``sizing`` refuses a step that leaves a pane too small. */
export function nudgeSplitDivider(
  target: SplitTarget,
  key: string,
  direction: TerminalPaneDirection,
  sizing: PaneSizing | null = null,
): void {
  target.update((current) => {
    if (!current) return current;
    const layout = nudgeTerminalSplit(current.layout, key, direction);
    if (layout === current.layout || !keepsRoom(current.layout, layout, sizing)) return current;
    return { ...current, layout };
  });
}

/** Equal panes, or as near to equal as the size rule lets the panes as they are go. */
export function equalizeSplit(target: SplitTarget, sizing: PaneSizing | null = null): void {
  target.update((current) => {
    if (!current) return current;
    const equal = equalizeTerminalLayout(current.layout);
    if (keepsRoom(current.layout, equal, sizing)) return { ...current, layout: equal };
    const layout = sizing ? sizing.toward(equal, current.layout) : current.layout;
    return layout === current.layout ? current : { ...current, layout };
  });
}

export function removeSplitPane(target: SplitTarget, key: string): void {
  target.update((current) => current && closeTerminalPane(current, key));
}
