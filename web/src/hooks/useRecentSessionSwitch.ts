// ⌃] / ⌃[ open a ⌘Tab-style recency switcher: presses while Control is held cycle it, releasing
// Control opens the selection, Esc cancels. macOS only, as Ctrl+[ / Ctrl+] are taken elsewhere;
// ⌘K opens it on every platform.

import { useEffect, useRef } from "react";
import { create } from "zustand";

import {
  currentSidebarSessionId,
  openSidebarSession,
  useSidebarSessionOrderStore,
} from "@/hooks/useSidebarSessionOrder";
import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isMacPlatform } from "@/lib/hotkeys";
import {
  type RecentSessionDirection,
  readRecentSessionHistory,
  recentSessionOrder,
  recordSessionVisit,
  steppedRecentIndex,
  writeRecentSessionHistory,
} from "@/lib/recentSessionHistory";
import { useNavigate } from "@/lib/routing";
import { matchesShortcut } from "@/lib/shortcuts/registry";

/** ⌃[ is ESC and ⌃] is GS to a terminal; the code editor binds them too. */
const CHORD_OWNING_SURFACES = ".xterm, .monaco-editor";
/** A focused command-menu input keeps the chord unless its menu claims it. */
const COMMAND_MENU_INPUT = "[cmdk-input]";

type Navigate = (path: string) => void;

/** The switcher while it is open. */
export interface RecentSessionSwitcher {
  /** Opened by holding Control, so releasing it opens the selection. False from ⌘K. */
  holding: boolean;
  /** The session open when the switcher opened. */
  fromSessionId: string | null;
  /** The direction of the press that opened it. */
  firstDirection: RecentSessionDirection;
  /** The frozen recency order; null until the sessions are known. */
  order: string[] | null;
  /** The session a release, Enter, or click opens; null until the order is known. */
  selectedId: string | null;
  /** Presses after the first one, made before the order was known. */
  pendingSteps: RecentSessionDirection[];
  /** Released before the order was known: open the selection as soon as it is. */
  commitWhenKnown: boolean;
  navigate: Navigate;
}

interface RecentSessionSwitchState {
  /** Visited session ids, most recent first. */
  history: string[];
  /**
   * Reads every live session the client knows of, whatever the sidebar's filter
   * or collapsed folders hide. Only these can be switched to.
   */
  readLiveSessionIds: (() => ReadonlySet<string>) | null;
  /** True once the session lists have loaded, or failed to, so an empty list means none. */
  sessionsKnown: boolean;
  /** True while the session lists failed to load: known, but an empty list proves nothing. */
  sessionsFailed: boolean;
  switcher: RecentSessionSwitcher | null;
  /** A surface layered over the sidebar (the open ⌘K palette) that takes the chord. */
  chordOwner: ((direction: RecentSessionDirection) => void) | null;
}

export const useRecentSessionSwitchStore = create<RecentSessionSwitchState>(() => ({
  history: readRecentSessionHistory(),
  readLiveSessionIds: null,
  sessionsKnown: false,
  sessionsFailed: false,
  switcher: null,
  chordOwner: null,
}));

const NO_LIVE_SESSIONS: ReadonlySet<string> = new Set();

/** The live sessions recency can switch to right now. */
export function liveRecentSessionIds(): ReadonlySet<string> {
  return useRecentSessionSwitchStore.getState().readLiveSessionIds?.() ?? NO_LIVE_SESSIONS;
}

/** The live sessions the switcher's order holds: visited ones, then the sidebar's rows. */
export function reachableRecentSessionIds(): string[] {
  const live = liveRecentSessionIds();
  return recentSessionOrder(
    useRecentSessionSwitchStore.getState().history,
    useSidebarSessionOrderStore.getState().orderedSessionIds,
    (id) => live.has(id),
  );
}

/** Make `sessionId` the most recent visit. */
export function recordRecentSessionVisit(sessionId: string): void {
  const history = recordSessionVisit(useRecentSessionSwitchStore.getState().history, sessionId);
  useRecentSessionSwitchStore.setState({ history });
  writeRecentSessionHistory(history);
}

/**
 * The recency order to switch through, frozen as the switcher opens: visited sessions still
 * live, most recent first, then the rest in sidebar order. Null while the sessions aren't known.
 */
function knownRecentOrder(): string[] | null {
  const { history, readLiveSessionIds, sessionsKnown } = useRecentSessionSwitchStore.getState();
  if (!readLiveSessionIds || !sessionsKnown) return null;
  const live = readLiveSessionIds();
  return recentSessionOrder(
    history,
    useSidebarSessionOrderStore.getState().orderedSessionIds,
    (id) => live.has(id),
  );
}

/** The selection after `steps` presses, starting one press from the open session. */
function selectionAfter(
  order: readonly string[],
  switcher: Pick<RecentSessionSwitcher, "fromSessionId" | "firstDirection">,
  steps: readonly RecentSessionDirection[],
): string | null {
  const live = liveRecentSessionIds();
  const fromIndex = switcher.fromSessionId ? order.indexOf(switcher.fromSessionId) : -1;
  // Off the order (a new session, an archived one), ⌃] selects the latest visit itself.
  let index: number | undefined =
    fromIndex !== -1 ? fromIndex : switcher.firstDirection === 1 ? -1 : 0;
  for (const direction of [switcher.firstDirection, ...steps]) {
    // The order is frozen, but a session deleted or archived since is passed over.
    index = steppedRecentIndex(order, index, direction, (id) => live.has(id));
    if (index === undefined) return null;
  }
  return order[index] ?? null;
}

/**
 * Open the switcher one step from the open session, or step it if it is open.
 * With `holding`, releasing Control opens the selection. Returns false when the
 * sessions are known and none can be switched to.
 */
export function openRecentSessionSwitcher(
  direction: RecentSessionDirection,
  { holding, navigate }: { holding: boolean; navigate: Navigate },
): boolean {
  const open = useRecentSessionSwitchStore.getState().switcher;
  if (open) {
    stepRecentSessionSwitcher(direction);
    if (holding && !open.holding) updateSwitcher({ holding: true });
    return true;
  }
  const fromSessionId = currentSidebarSessionId() ?? null;
  // The open session is the newest visit, so one step reaches the one before it.
  if (fromSessionId) recordRecentSessionVisit(fromSessionId);
  const order = knownRecentOrder();
  const base = { fromSessionId, firstDirection: direction };
  const selectedId = order ? selectionAfter(order, base, []) : null;
  if (order && !selectedId) return false;
  useRecentSessionSwitchStore.setState({
    switcher: {
      ...base,
      holding,
      order,
      selectedId,
      pendingSteps: [],
      commitWhenKnown: false,
      navigate,
    },
  });
  return true;
}

function updateSwitcher(patch: Partial<RecentSessionSwitcher>): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (switcher) useRecentSessionSwitchStore.setState({ switcher: { ...switcher, ...patch } });
}

/** Move the selection one step, wrapping, or remember the press until the order is known. */
export function stepRecentSessionSwitcher(direction: RecentSessionDirection): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (!switcher) return;
  if (!switcher.order) {
    updateSwitcher({ pendingSteps: [...switcher.pendingSteps, direction] });
    return;
  }
  const live = liveRecentSessionIds();
  const from = switcher.selectedId ? switcher.order.indexOf(switcher.selectedId) : -1;
  const index = steppedRecentIndex(switcher.order, from, direction, (id) => live.has(id));
  if (index !== undefined) updateSwitcher({ selectedId: switcher.order[index]! });
}

/** Select a row directly, such as with the arrow keys or the pointer. */
export function selectRecentSession(sessionId: string): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (switcher?.order?.includes(sessionId)) updateSwitcher({ selectedId: sessionId });
}

/**
 * Freeze the order for a switcher opened before the sessions were known, replaying the presses
 * and any release made while they loaded.
 */
function resolvePendingSwitcher(): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (!switcher || switcher.order) return;
  const order = knownRecentOrder();
  if (!order) return;
  const selectedId = selectionAfter(order, switcher, switcher.pendingSteps);
  if (!selectedId) {
    cancelRecentSessionSwitcher();
    return;
  }
  updateSwitcher({ order, selectedId, pendingSteps: [] });
  if (switcher.commitWhenKnown) commitRecentSessionSwitcher();
}

/**
 * The selection if it is still live, else the next live row in the direction
 * of travel; null when none is left. The frozen order itself never changes.
 */
function liveSelection(switcher: RecentSessionSwitcher): string | null {
  const { order, selectedId } = switcher;
  if (!order || !selectedId) return null;
  const live = liveRecentSessionIds();
  if (live.has(selectedId)) return selectedId;
  const index = steppedRecentIndex(
    order,
    order.indexOf(selectedId),
    switcher.firstDirection,
    (id) => live.has(id),
  );
  return index === undefined ? null : order[index]!;
}

/**
 * Keep an open switcher on a live session after the session lists change, such
 * as when the selected session is archived or deleted; close it if none is left.
 */
export function reconcileRecentSessionSwitcher(): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (!switcher?.order) return;
  const selectedId = liveSelection(switcher);
  if (!selectedId) cancelRecentSessionSwitcher();
  else if (selectedId !== switcher.selectedId) updateSwitcher({ selectedId });
}

/** Open `sessionId`, or the selection, and close the switcher. Only a live session opens. */
export function commitRecentSessionSwitcher(sessionId?: string): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (!switcher) return;
  const targetId =
    sessionId === undefined
      ? liveSelection(switcher)
      : liveRecentSessionIds().has(sessionId)
        ? sessionId
        : null;
  useRecentSessionSwitchStore.setState({ switcher: null });
  if (!targetId) return;
  recordRecentSessionVisit(targetId);
  if (targetId !== currentSidebarSessionId()) openSidebarSession(targetId, switcher.navigate);
}

/** Whether the switcher is open, such as for a chord that should drop it first. */
export function isRecentSessionSwitcherOpen(): boolean {
  return useRecentSessionSwitchStore.getState().switcher !== null;
}

/** Close the switcher and stay on the open session. */
export function cancelRecentSessionSwitcher(): void {
  if (useRecentSessionSwitchStore.getState().switcher) {
    useRecentSessionSwitchStore.setState({ switcher: null });
  }
}

/** Control was released: open the selection, or as soon as the sessions are known. */
export function releaseRecentSessionSwitcher(): void {
  const { switcher } = useRecentSessionSwitchStore.getState();
  if (!switcher?.holding) return;
  if (switcher.order) commitRecentSessionSwitcher();
  else updateSwitcher({ commitWhenKnown: true });
}

/**
 * Provides the live sessions recency can switch to, and whether their lists
 * have loaded yet. A switcher opened before then picks its order once they have.
 */
export function useRegisterLiveSessions(
  readLiveSessionIds: () => ReadonlySet<string>,
  sessionsKnown = true,
  sessionsFailed = false,
): void {
  useEffect(() => {
    useRecentSessionSwitchStore.setState({ readLiveSessionIds });
    return () => {
      if (useRecentSessionSwitchStore.getState().readLiveSessionIds === readLiveSessionIds) {
        useRecentSessionSwitchStore.setState({ readLiveSessionIds: null });
      }
    };
  }, [readLiveSessionIds]);
  useEffect(() => {
    useRecentSessionSwitchStore.setState({ sessionsKnown, sessionsFailed });
    if (sessionsKnown) resolvePendingSwitcher();
  }, [sessionsKnown, sessionsFailed, readLiveSessionIds]);
}

/** The switch ⌃] / ⌃[ asks for, or null for any other key. */
export function recentSessionChordDirection(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): RecentSessionDirection | null {
  if (matchesShortcut(event, "session.recent", { mac: isMac })) return 1;
  if (matchesShortcut(event, "session.recentReverse", { mac: isMac })) return -1;
  return null;
}

/** Records each session the user opens. */
export function useRecordRecentSessionVisits(activeSessionId: string | undefined): void {
  useEffect(() => {
    if (activeSessionId) recordRecentSessionVisit(activeSessionId);
  }, [activeSessionId]);
}

/**
 * While `enabled`, the caller owns ⌃] / ⌃[: the binding hands it the switch
 * instead of switching itself, so an overlay can dismiss itself first.
 */
export function useClaimRecentSessionChord(
  enabled: boolean,
  onSwitch: (direction: RecentSessionDirection) => void,
): void {
  const latest = useRef(onSwitch);
  latest.current = onSwitch;

  useEffect(() => {
    if (!enabled) return;
    const owner = (direction: RecentSessionDirection) => latest.current(direction);
    useRecentSessionSwitchStore.setState({ chordOwner: owner });
    return () => {
      if (useRecentSessionSwitchStore.getState().chordOwner === owner) {
        useRecentSessionSwitchStore.setState({ chordOwner: null });
      }
    };
  }, [enabled]);
}

/** Bind once, with the QuickPick palette, beside the sidebar that publishes the session order. */
export function useRecentSessionHotkey(isMac = isMacPlatform()): void {
  // Navigating changes navigate's identity; re-binding then would drop a held switch.
  const navigate = useNavigate();
  const latestNavigate = useRef(navigate);
  latestNavigate.current = navigate;
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!isMac) return;
    // Capture phase, so a focused composer or widget can't consume the chord first.
    const onKeyDown = (event: globalThis.KeyboardEvent): void => {
      const direction = recentSessionChordDirection(event, isMac);
      if (direction === null) {
        // Any key but Esc without Control means the hold is over, even if its keyup was missed.
        if (!event.ctrlKey && event.key !== "Escape") releaseRecentSessionSwitcher();
        return;
      }
      // Embedded in a host page, a chord from outside the embed is the host's.
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      const focused = document.activeElement instanceof Element ? document.activeElement : null;
      if (focused?.closest(CHORD_OWNING_SURFACES)) return;

      const { chordOwner, switcher, sessionsKnown } = useRecentSessionSwitchStore.getState();
      if (!switcher) {
        if (!chordOwner && focused?.closest(COMMAND_MENU_INPUT)) return;
        // Known to be empty: leave the chord to whatever else wants it.
        if (sessionsKnown && liveRecentSessionIds().size === 0) return;
      }
      event.preventDefault();
      event.stopPropagation();
      // A held chord steps once per press, not once per key repeat.
      if (event.repeat) return;
      if (chordOwner && !switcher) chordOwner(direction);
      else
        openRecentSessionSwitcher(direction, { holding: true, navigate: latestNavigate.current });
    };
    const onKeyUp = (event: globalThis.KeyboardEvent): void => {
      if (event.key === "Control") releaseRecentSessionSwitcher();
    };
    // A release the window never saw, such as with focus in a webview or a
    // terminal: the next pointer event in this window shows Control is up.
    const onPointer = (event: PointerEvent): void => {
      if (!event.ctrlKey && document.hasFocus()) releaseRecentSessionSwitcher();
    };
    // Only the focused window opens a session; one losing focus just closes its switcher.
    const onVisibilityChange = (): void => {
      if (document.visibilityState === "hidden") cancelRecentSessionSwitcher();
    };

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("pointermove", onPointer, true);
    window.addEventListener("pointerdown", onPointer, true);
    window.addEventListener("blur", cancelRecentSessionSwitcher);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("pointermove", onPointer, true);
      window.removeEventListener("pointerdown", onPointer, true);
      window.removeEventListener("blur", cancelRecentSessionSwitcher);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      cancelRecentSessionSwitcher();
    };
  }, [isMac, isEmbedded]);
}
