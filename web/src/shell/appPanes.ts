// App panes: a workspace tab shown as an `app:<tabId>` pane of the session's split. The pane hosts
// the sidebar's own content for the tab, so the tab keeps its state as it moves.

import type { LucideIcon } from "lucide-react";
import { useSyncExternalStore } from "react";

import {
  closeTerminalPane,
  replaceTerminalLayoutKey,
  terminalLayoutKeys,
  type TerminalPaneSide,
  type TerminalRect,
  type TerminalSplitAxis,
} from "@/lib/paneSplitLayout";

import {
  type PaneDragOutside,
  paneDropSpot,
  placedLayout,
  placedPaneArea,
  splitOfSide,
  splitPanesRoot,
} from "./paneMove";
import { NO_ROOM_MESSAGE } from "./paneSizing";
import { requestTerminalFocus } from "./terminalFocusRequest";
import {
  focusSplitPane,
  insertSplitPane,
  measureSplitSizing,
  roomToSplit,
} from "./splitTargetActions";
import { zoomedPaneKey } from "./terminalPaneZoom";
import { mainSplitTarget } from "./terminalSplitTarget";
import type { TabId } from "./workspaceTabs/workspacePlace";

const APP_PANE_PREFIX = "app:";

/** The split key of the pane showing workspace tab `tabId`. */
export const appPaneKey = (tabId: TabId): string => `${APP_PANE_PREFIX}${tabId}`;

/** The workspace tab a split key shows, or null for a terminal's pane. */
export function appPaneTabId(key: string): TabId | null {
  return key.startsWith(APP_PANE_PREFIX) ? key.slice(APP_PANE_PREFIX.length) : null;
}

export const isAppPaneKey = (key: string): boolean => key.startsWith(APP_PANE_PREFIX);

export { APP_PANE_HOST, APP_PANE_HOST_ATTRIBUTE } from "@/lib/paneSurfaces";

/** What an app pane's row shows: the tab's own title and icon, a file's type icon for a file. */
export interface AppPaneTitle {
  label: string;
  /** The tab's full title, such as a pull request's repository, number and title. */
  tooltip?: string;
  icon: LucideIcon;
  filePath?: string;
}

interface SessionAppPanes {
  hosts: ReadonlyMap<TabId, HTMLElement>;
  titles: ReadonlyMap<TabId, AppPaneTitle>;
}

const EMPTY: SessionAppPanes = { hosts: new Map(), titles: new Map() };
const sessions = new Map<string, SessionAppPanes>();
const listeners = new Set<() => void>();

function update(conversationId: string, change: (current: SessionAppPanes) => SessionAppPanes) {
  const current = sessions.get(conversationId) ?? EMPTY;
  const next = change(current);
  if (next === current) return;
  sessions.set(conversationId, next);
  for (const listener of listeners) listener();
}

function withEntry<Value>(
  map: ReadonlyMap<TabId, Value>,
  tabId: TabId,
  value: Value | null,
  same: (a: Value, b: Value) => boolean = Object.is,
): ReadonlyMap<TabId, Value> {
  const existing = map.get(tabId);
  if (value === null ? existing === undefined : existing !== undefined && same(existing, value)) {
    return map;
  }
  const next = new Map(map);
  if (value === null) next.delete(tabId);
  else next.set(tabId, value);
  return next;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const snapshotOf = (conversationId: string | null | undefined): SessionAppPanes =>
  (conversationId && sessions.get(conversationId)) || EMPTY;

/** Register `element` as the pane where workspace tab `tabId` renders, or clear it. */
export function setAppPaneHost(conversationId: string, tabId: TabId, element: HTMLElement | null) {
  update(conversationId, (current) => {
    const hosts = withEntry(current.hosts, tabId, element);
    return hosts === current.hosts ? current : { ...current, hosts };
  });
}

/** The panes showing this session's workspace tabs, by tab: where the sidebar renders each. */
export const useAppPaneHosts = (conversationId: string | null | undefined) =>
  useSyncExternalStore(subscribe, () => snapshotOf(conversationId).hosts);

/** Publish the title of a tab shown as a pane, or clear it. */
export function setAppPaneTitle(conversationId: string, tabId: TabId, title: AppPaneTitle | null) {
  update(conversationId, (current) => {
    const titles = withEntry(
      current.titles,
      tabId,
      title,
      (a, b) =>
        a.label === b.label &&
        a.tooltip === b.tooltip &&
        a.icon === b.icon &&
        a.filePath === b.filePath,
    );
    return titles === current.titles ? current : { ...current, titles };
  });
}

/** The titles of this session's tabs shown as panes, by tab. */
export const useAppPaneTitles = (conversationId: string | null | undefined) =>
  useSyncExternalStore(subscribe, () => snapshotOf(conversationId).titles);

/** Whether workspace tab `tabId` shows as a pane of the session's split right now. */
export function isTabShownAsPane(conversationId: string, tabId: TabId): boolean {
  return (
    snapshotOf(conversationId).hosts.has(tabId) && isInMainSplit(conversationId, appPaneKey(tabId))
  );
}

/** Bring the pane showing tab `tabId` forward: it becomes the focused pane and takes the keyboard. */
export function focusAppPane(conversationId: string, tabId: TabId): void {
  const key = appPaneKey(tabId);
  focusSplitPane(mainSplitTarget(conversationId), key);
  requestTerminalFocus({ conversationId, surface: "main", tabKey: key });
}

/** The workspace tabs the session's split shows as panes. */
export function appPaneTabIds(layoutKeys: readonly string[]): TabId[] {
  return layoutKeys.flatMap((key) => {
    const tabId = appPaneTabId(key);
    return tabId === null ? [] : [tabId];
  });
}

/** Where a tab opens as a pane: beside `besideKey`, else the split's focused pane. */
export interface AppPanePlacement {
  /** The agent's terminal, which the split starts from while unsplit. */
  agentKey: string | null;
  axis?: TerminalSplitAxis;
  besideKey?: string;
}

/** Whether the session's split shows the pane keyed `key` now. */
export function isInMainSplit(conversationId: string, key: string): boolean {
  const layout = mainSplitTarget(conversationId).read()?.layout;
  return layout !== undefined && terminalLayoutKeys(layout).includes(key);
}

/**
 * Why a pane keyed `newKey` (null: one yet to open) can't open beside the split's focused pane
 * now: the panes would go below the size every pane keeps, as for a terminal split. Undefined
 * when it can, or when the split already shows it.
 */
export function paneRoomRefusal(
  conversationId: string,
  newKey: string | null,
  { agentKey, axis = "columns", besideKey }: AppPanePlacement,
): string | undefined {
  const target = mainSplitTarget(conversationId);
  const current = target.read();
  if (newKey !== null && isInMainSplit(conversationId, newKey)) return undefined;
  const sourceKey = besideKey ?? current?.focusedKey ?? agentKey;
  if (sourceKey === null || roomToSplit(target, sourceKey, axis, "after", agentKey)) {
    return undefined;
  }
  return measureSplitSizing(target)?.refusal(axis) ?? NO_ROOM_MESSAGE;
}

/** Why tab `tabId` (null: a tab yet to open) can't open as a pane now; see paneRoomRefusal. */
export const appPaneRoomRefusal = (
  conversationId: string,
  tabId: TabId | null,
  placement: AppPanePlacement,
): string | undefined =>
  paneRoomRefusal(conversationId, tabId === null ? null : appPaneKey(tabId), placement);

/**
 * Show workspace tab `tabId` as a pane beside `besideKey`, else the split's focused pane, or the
 * agent's terminal while unsplit, taking focus. False when there's no pane to put it beside, or no room for it.
 */
export const openTabAsPane = (
  conversationId: string,
  tabId: TabId,
  placement: AppPanePlacement,
): boolean => openPaneInSplit(conversationId, appPaneKey(tabId), placement);

/**
 * Add the pane keyed `newKey` beside `besideKey`, else the split's focused pane, or the agent's
 * terminal while unsplit, and focus it; one the split already shows is focused. False when
 * there's no pane to put it beside, or no room for it.
 */
export function openPaneInSplit(
  conversationId: string,
  newKey: string,
  { agentKey, axis = "columns", besideKey }: AppPanePlacement,
): boolean {
  const target = mainSplitTarget(conversationId);
  const current = target.read();
  if (current && terminalLayoutKeys(current.layout).includes(newKey)) {
    target.update((state) => state && { ...state, focusedKey: newKey });
    return true;
  }
  const sourceKey = besideKey ?? current?.focusedKey ?? agentKey;
  if (sourceKey === null || !roomToSplit(target, sourceKey, axis, "after", agentKey)) return false;
  const inserted = insertSplitPane(target, {
    sourceKey,
    newKey,
    axis,
    unsplitKey: agentKey,
    takeFocus: false,
  });
  if (inserted) target.update((state) => state && { ...state, focusedKey: newKey });
  return inserted;
}

// Stands for the dropped tab's pane while working out where it would land.
const DROPPED_PANE = "\u0000dropped";

/** Where a sidebar tab dragged over the session's panes would land: beside `targetKey`. */
export interface AppPaneDrop {
  targetKey: string;
  side: TerminalPaneSide;
  /** Where the tab's pane would sit once dropped, in percent of the panes' view. */
  area: TerminalRect;
}

/**
 * The spot a sidebar tab dropped at `point` (client pixels) would take among the session's
 * panes, or null off them, while one is maximized and a new pane wouldn't show, or where the
 * panes would go below the size every pane keeps.
 */
export function appPaneDropAt(
  conversationId: string,
  point: { x: number; y: number },
  agentKey: string | null,
): AppPaneDrop | null {
  const root = splitPanesRoot(conversationId);
  const target = mainSplitTarget(conversationId);
  const layout = target.read()?.layout ?? agentKey;
  if (!root || layout === null || zoomedPaneKey(target) !== null) return null;
  const box = root.getBoundingClientRect();
  if (box.width <= 0 || box.height <= 0) return null;
  // The spot the preview shows now stays until the pointer is clearly in another zone.
  const spot = paneDropSpot(
    layout,
    { x: ((point.x - box.left) / box.width) * 100, y: ((point.y - box.top) / box.height) * 100 },
    pendingDrops.get(conversationId) ?? null,
  );
  if (!spot) return null;
  const { axis, placement } = splitOfSide(spot.side);
  if (!roomToSplit(target, spot.targetKey, axis, placement, agentKey)) return null;
  const area = placedPaneArea(layout, {
    kind: "insert",
    key: DROPPED_PANE,
    targetKey: spot.targetKey,
    side: spot.side,
  });
  return area ? { ...spot, area } : null;
}

/** Show workspace tab `tabId` as a pane on `drop`'s side of its target, taking focus. */
export const dropTabAsPane = (
  conversationId: string,
  tabId: TabId,
  drop: Pick<AppPaneDrop, "targetKey" | "side">,
  agentKey: string | null,
): boolean => dropPaneInSplit(conversationId, appPaneKey(tabId), drop, agentKey);

/** Add the pane keyed `newKey` on `drop`'s side of its target and focus it; false if already shown. */
export function dropPaneInSplit(
  conversationId: string,
  newKey: string,
  drop: Pick<AppPaneDrop, "targetKey" | "side">,
  agentKey: string | null,
): boolean {
  const target = mainSplitTarget(conversationId);
  if (isInMainSplit(conversationId, newKey)) return false;
  // The panes may have changed since the preview: a maximized pane offers no spot, as the preview
  // doesn't, and the room is checked again.
  if (zoomedPaneKey(target) !== null) return false;
  const { axis, placement } = splitOfSide(drop.side);
  if (!roomToSplit(target, drop.targetKey, axis, placement, agentKey)) return false;
  let inserted = false;
  target.update((current) => {
    const state =
      current ?? (agentKey === null ? null : { layout: agentKey, focusedKey: agentKey });
    if (state === null) return current;
    // The same placement the preview read, so the pane lands where it showed.
    const layout = placedLayout(state.layout, {
      kind: "insert",
      key: newKey,
      targetKey: drop.targetKey,
      side: drop.side,
    });
    if (layout === state.layout) return current;
    inserted = true;
    return { layout, focusedKey: newKey };
  });
  return inserted;
}

// The spot a sidebar tab dragged over the panes would take, by session, for the panes' preview.
const pendingDrops = new Map<string, AppPaneDrop>();

/** Show where the tab being dragged would land, or clear it. */
export function setPendingAppPaneDrop(conversationId: string, drop: AppPaneDrop | null) {
  const current = pendingDrops.get(conversationId) ?? null;
  if (current === drop) return;
  if (
    current &&
    drop &&
    current.targetKey === drop.targetKey &&
    current.side === drop.side &&
    current.area.left === drop.area.left &&
    current.area.top === drop.area.top &&
    current.area.width === drop.area.width &&
    current.area.height === drop.area.height
  ) {
    return;
  }
  if (drop) pendingDrops.set(conversationId, drop);
  else pendingDrops.delete(conversationId);
  for (const listener of dropListeners) listener();
}

// A drag notifies on every pointer move, so only the drop's own readers hear it.
const dropListeners = new Set<() => void>();

function subscribeDrops(listener: () => void): () => void {
  dropListeners.add(listener);
  return () => dropListeners.delete(listener);
}

/** Where a sidebar tab dragged over this session's panes would land, if one is. */
export const usePendingAppPaneDrop = (conversationId: string | null | undefined) =>
  useSyncExternalStore(subscribeDrops, () =>
    conversationId ? (pendingDrops.get(conversationId) ?? null) : null,
  );

/**
 * A tab shown as a pane became tab `to`, as a New tab does when filled: the pane shows `to`,
 * or goes where `to` already has a pane of its own. A shell, `toTerminalKey`, shows in the
 * split as its own terminal pane, since shells don't render in an app pane.
 */
export function followReplacedAppPane(
  conversationId: string,
  from: TabId,
  to: TabId,
  toTerminalKey?: string,
): void {
  const fromKey = appPaneKey(from);
  const toKey = toTerminalKey ?? appPaneKey(to);
  // A New tab filled in place keeps its id, and so its pane.
  if (fromKey === toKey) return;
  mainSplitTarget(conversationId).update((current) => {
    if (!current) return current;
    const keys = terminalLayoutKeys(current.layout);
    if (!keys.includes(fromKey)) return current;
    if (keys.includes(toKey)) {
      const layout = closeTerminalPane(current, fromKey);
      return layout && { ...layout, focusedKey: toKey };
    }
    return {
      layout: replaceTerminalLayoutKey(current.layout, fromKey, toKey),
      focusedKey: current.focusedKey === fromKey ? toKey : current.focusedKey,
    };
  });
}

/** What the workspace sidebar does with a tab shown as a pane, as its own strip would. */
export interface AppPaneTabActions {
  /** Close the tab, asking first where it has unsaved edits; its pane goes with it. */
  close: (tabId: TabId) => void;
  /** Put the tab back in the sidebar, selected there. */
  moveToSidebar: (tabId: TabId) => void;
}

const tabActions = new Map<string, AppPaneTabActions>();

/** Register how the session's sidebar closes and takes back its pane tabs, or clear it. */
export function setAppPaneTabActions(conversationId: string, actions: AppPaneTabActions | null) {
  if (actions) tabActions.set(conversationId, actions);
  else tabActions.delete(conversationId);
}

/** How the session's sidebar acts on its pane tabs, or null without a sidebar to take them. */
export const appPaneTabActions = (conversationId: string): AppPaneTabActions | null =>
  tabActions.get(conversationId) ?? null;

const WORKSPACE_SIDEBAR = 'aside[aria-label="Workspace"]';

/** The workspace sidebar under `point`, if it shows there. */
function workspaceSidebarAt(point: { x: number; y: number }): HTMLElement | null {
  const sidebar = document
    .elementFromPoint(point.x, point.y)
    ?.closest<HTMLElement>(WORKSPACE_SIDEBAR);
  return sidebar ?? null;
}

/** A pane other than an app pane that its row can drag onto the workspace sidebar: a shell's. */
export interface SidebarPaneMover {
  /** Whether the pane keyed `key` may move to the sidebar now. */
  canMove: (key: string) => boolean;
  /** Move it there, rechecking that it may; false when it can't. */
  move: (key: string) => boolean;
}

/**
 * An app pane dragged by its row onto the workspace sidebar goes back there, as does a pane
 * `mover` moves, such as a shell's; the sidebar shows it would take the pane while the pointer
 * is over it.
 */
export function appPaneSidebarDrop(
  conversationId: string,
  mover?: SidebarPaneMover,
): PaneDragOutside {
  const movable = (key: string) => isAppPaneKey(key) || (mover?.canMove(key) ?? false);
  const mark = (sidebar: HTMLElement | null) => {
    for (const marked of document.querySelectorAll<HTMLElement>("[data-app-pane-drop-target]")) {
      if (marked !== sidebar) delete marked.dataset.appPaneDropTarget;
    }
    if (sidebar) sidebar.dataset.appPaneDropTarget = "";
  };
  return {
    over: (key, point) => {
      mark(point && movable(key) ? workspaceSidebarAt(point) : null);
    },
    drop: (key, point) => {
      mark(null);
      if (!isAppPaneKey(key)) {
        return !!mover && mover.canMove(key) && !!workspaceSidebarAt(point) && mover.move(key);
      }
      const tabId = appPaneTabId(key);
      const actions = appPaneTabActions(conversationId);
      if (tabId === null || !actions || !workspaceSidebarAt(point)) return false;
      actions.moveToSidebar(tabId);
      return true;
    },
  };
}

/** Forget every session's panes, for tests. */
export function resetAppPanes(): void {
  sessions.clear();
  tabActions.clear();
  pendingDrops.clear();
}
