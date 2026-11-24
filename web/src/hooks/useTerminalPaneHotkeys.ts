// A panes view's keys and ⌘K commands: split, move between, resize, equalize, maximize, swap,
// move and close panes. A key is claimed only while one of this view's terminals, or its chat
// pane, has the keyboard, so editors and fields keep theirs; ⌘K acts on the split's focused pane.

import { type RefObject, useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import {
  ArrowDownIcon,
  ArrowLeftIcon,
  ArrowLeftRightIcon,
  ArrowRightIcon,
  ArrowUpDownIcon,
  ArrowUpIcon,
  ChevronsDownIcon,
  ChevronsLeftIcon,
  ChevronsRightIcon,
  ChevronsUpIcon,
  Columns2Icon,
  EqualIcon,
  type LucideIcon,
  Maximize2Icon,
  Minimize2Icon,
  PanelTopIcon,
  Rows2Icon,
  XIcon,
} from "lucide-react";

import {
  isComposerSteerAllKey,
  readSubmitWithModEnter,
} from "@/lib/composerSendShortcutPreferences";
import { isFocusInDialogOrMenu } from "@/lib/hotkeys";
import {
  terminalLayoutKeys,
  type TerminalPaneDirection,
  type TerminalSplitState,
} from "@/lib/paneSplitLayout";
import { paneCommandLabel, PANE_ZOOM_LABELS } from "@/shell/paneCommandTable";
import { domPaneFocus, focusInChatPane, type PaneFocus } from "@/shell/paneFocus";
import {
  movePaneCommand,
  moveSplitPane,
  paneMoveLayout,
  paneMoveOptions,
  type PaneMoveSource,
} from "@/shell/paneMove";
import {
  matchedPaneShortcut,
  paneShortcutChords,
  paneShortcutLabel,
  type PaneShortcutId,
} from "@/shell/paneShortcuts";
import { showPaneToolbar } from "@/shell/paneStrip/paneToolbarFocus";
import type { PaneMenuChoice, SplitPaneCommands } from "@/shell/paneStrip/paneMenu";
import { PANE_SWAP_IDS, paneSwapDirections, paneSwapLabel, swapSplitPane } from "@/shell/paneSwap";
import {
  equalizeSplit,
  moveSplitFocus,
  nudgeSplitDivider,
  PANE_CLOSED_MESSAGE,
  SPLIT_CLOSING_MESSAGE,
} from "@/shell/splitTargetActions";
import type { PaneCommands } from "@/shell/terminalPaneActions";
import { togglePaneZoom, useZoomedPaneKey, zoomedPaneKey } from "@/shell/terminalPaneZoom";
import type { SplitTarget } from "@/shell/terminalSplitTarget";
import { usePaneMoveAvailability } from "@/shell/usePaneMoveAvailability";
import { showToast } from "@/components/ui/toast";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  type PaletteCommand,
  usePaletteCommands,
} from "@/store/paletteCommands";

export const SPLIT_PENDING_REASON = "Wait for the new pane to open.";

/** The ⌘K heading the panes' commands are listed under. */
export const PANE_COMMANDS_HEADING = "Panes";

export interface TerminalPaneHotkeysOptions {
  /** The split these keys act on: the main view's, or a sidebar group's. */
  target: SplitTarget;
  /** This view shows its panes and may change them. */
  enabled: boolean;
  split: TerminalSplitState | undefined;
  /** The pane shown unsplit, which a first split divides. */
  singlePaneKey: string | null;
  /**
   * The pane the web chat is, while it shows, and the session it's for: with the keyboard in that
   * session's chat, keys act on it.
   */
  chatPane?: { key: string; sessionId: string } | null;
  /** ⌘K lists this view's commands until another view's panes take the keyboard. */
  defaultForPalette?: boolean;
  actions: PaneCommands;
  /** The element holding the panes, which scopes the keys and gives the aspect. */
  panesRef: RefObject<HTMLElement | null>;
  /** Where the panes' focus is and how it moves; their DOM focus under ``panesRef`` by default. */
  paneFocus?: PaneFocus;
  /** The name a pane's row shows, for Move Pane's spots; the pane key otherwise. */
  paneName?: (paneKey: string) => string;
}

/** The keys the web chat lends its pane; the rest stay the chat's. */
/** A chord the composer reads as its own, such as ⇧⌘↩ steering the queue when ⌘↩ sends. */
const composerOwns = (event: globalThis.KeyboardEvent): boolean =>
  isComposerSteerAllKey(event, readSubmitWithModEnter(), false);

const CHAT_PANE_KEYS: ReadonlySet<PaneShortcutId> = new Set<PaneShortcutId>([
  "terminal.splitRight",
  "terminal.splitDown",
  "terminal.toggleZoom",
  "terminal.focusLeft",
  "terminal.focusRight",
  "terminal.focusUp",
  "terminal.focusDown",
]);

const DIRECTIONS: readonly TerminalPaneDirection[] = ["left", "right", "up", "down"];
const FOCUS_IDS = {
  left: "terminal.focusLeft",
  right: "terminal.focusRight",
  up: "terminal.focusUp",
  down: "terminal.focusDown",
} as const satisfies Record<TerminalPaneDirection, PaneShortcutId>;
const RESIZE_IDS = {
  left: "terminal.resizeLeft",
  right: "terminal.resizeRight",
  up: "terminal.resizeUp",
  down: "terminal.resizeDown",
} as const satisfies Record<TerminalPaneDirection, PaneShortcutId>;
const RESIZE_KEYS: ReadonlySet<PaneShortcutId> = new Set(Object.values(RESIZE_IDS));
const FOCUS_ICONS: Record<TerminalPaneDirection, LucideIcon> = {
  left: ArrowLeftIcon,
  right: ArrowRightIcon,
  up: ArrowUpIcon,
  down: ArrowDownIcon,
};
const RESIZE_ICONS: Record<TerminalPaneDirection, LucideIcon> = {
  left: ChevronsLeftIcon,
  right: ChevronsRightIcon,
  up: ChevronsUpIcon,
  down: ChevronsDownIcon,
};

// The view whose panes last had the keyboard lists its commands in ⌘K, so two views' rows
// never show at once.
let paletteOwner: string | null = null;
const ownerListeners = new Set<() => void>();
function setPaletteOwner(id: string | null): void {
  if (paletteOwner === id) return;
  paletteOwner = id;
  for (const listener of ownerListeners) listener();
}
function subscribeOwner(listener: () => void): () => void {
  ownerListeners.add(listener);
  return () => ownerListeners.delete(listener);
}

/**
 * Whether the split ``targetId`` lists its commands in ⌘K now: it's the view whose panes last
 * had the keyboard, or no view has yet and it's the default one.
 */
export function usePaneCommandsListed(
  targetId: string,
  enabled: boolean,
  defaultForPalette: boolean,
): boolean {
  const owner = useSyncExternalStore(subscribeOwner, () => paletteOwner);
  return enabled && (owner === targetId || (owner === null && defaultForPalette));
}

/** Forget which view lists its commands, for tests. */
export function resetPanePaletteOwnerForTests(): void {
  setPaletteOwner(null);
}

const availability = (available: boolean, reason?: string): CommandAvailability => {
  if (available) return AVAILABLE;
  return reason === undefined ? HIDDEN : { available: false, reason };
};

const NO_COMMANDS: PaletteCommand[] = [];

/** Bind the panes' keys and ⌘K commands; returns each pane's menu commands for its row. */
export function useTerminalPaneHotkeys({
  target,
  enabled,
  split,
  singlePaneKey,
  chatPane = null,
  defaultForPalette = false,
  actions,
  panesRef,
  paneFocus,
  paneName,
}: TerminalPaneHotkeysOptions): (paneKey: string) => SplitPaneCommands | null {
  const focus = useMemo(
    () => paneFocus ?? domPaneFocus(target, panesRef),
    [paneFocus, target, panesRef],
  );
  const paneKeys = !enabled
    ? []
    : split
      ? terminalLayoutKeys(split.layout)
      : singlePaneKey === null
        ? []
        : [singlePaneKey];
  const isSplit = enabled && paneKeys.length > 1;
  const zoomedKey = useZoomedPaneKey(target, enabled ? split : undefined);
  // The pane ⌘K acts on: the split's focused pane, or the lone pane unsplit.
  const targetKey = split?.focusedKey ?? singlePaneKey;
  const canSplit = enabled && targetKey !== null && actions.canSplit;
  const canClose = isSplit && targetKey !== null && actions.canClosePane(targetKey);
  const swapDirections =
    isSplit && targetKey !== null ? paneSwapDirections(split?.layout, zoomedKey, targetKey, 1) : [];

  const latest = useRef({ enabled, paneKeys, isSplit, actions, chatPane, paneName });
  latest.current = { enabled, paneKeys, isSplit, actions, chatPane, paneName };

  /** Whether ``paneKey`` is still one of this enabled view's panes. */
  const isLive = (paneKey: string) =>
    latest.current.enabled && latest.current.paneKeys.includes(paneKey);

  /** Run pane key ``id`` on ``paneKey``; false when it can't act now. */
  const runPaneKey = (id: PaneShortcutId, paneKey: string): boolean => {
    const live = latest.current;
    if (!isLive(paneKey)) return false;
    const direction = DIRECTIONS.find(
      (candidate) => FOCUS_IDS[candidate] === id || RESIZE_IDS[candidate] === id,
    );
    switch (id) {
      case "terminal.splitRight":
      case "terminal.splitDown": {
        if (!live.actions.canSplit || live.actions.splitting) return false;
        if (live.actions.closingSplit) {
          showToast(SPLIT_CLOSING_MESSAGE);
          return true;
        }
        focus.focus(paneKey);
        live.actions.splitPane(paneKey, id === "terminal.splitRight" ? "columns" : "rows");
        return true;
      }
      case "terminal.equalize":
        if (!live.isSplit) return false;
        equalizeSplit(target, focus.sizing());
        return true;
      case "terminal.toggleZoom":
        if (!live.isSplit) return false;
        togglePaneZoom(target, focus, paneKey);
        return true;
      default:
        if (!live.isSplit || direction === undefined) return false;
        if (RESIZE_KEYS.has(id)) nudgeSplitDivider(target, paneKey, direction, focus.sizing());
        else moveSplitFocus(target, paneKey, direction, focus.aspect());
        return true;
    }
  };
  const runPaneKeyRef = useRef(runPaneKey);
  runPaneKeyRef.current = runPaneKey;

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent): void => {
      // An IME composition owns its keys, including the one that starts it.
      if (event.isComposing || !latest.current.enabled) return;
      const id = matchedPaneShortcut(event);
      if (id === undefined || isFocusInDialogOrMenu()) return;
      const { chatPane: chat, isSplit: splitNow } = latest.current;
      const lent =
        chat !== null &&
        CHAT_PANE_KEYS.has(id) &&
        focusInChatPane(chat.sessionId) &&
        !composerOwns(event)
          ? chat.key
          : null;
      const paneKey = focus.focusedPane() ?? lent;
      if (paneKey === null) return;
      // A lone pane leaves ⇧⌘↩ to its terminal.
      if (id === "terminal.toggleZoom" && !splitNow) return;
      // Claimed even when it can't act now, so the chord never reaches the shell.
      event.preventDefault();
      event.stopPropagation();
      // Holding a split chord would open a shell per tick; a divider may keep moving.
      if (event.repeat && !RESIZE_KEYS.has(id)) return;
      runPaneKeyRef.current(id, paneKey);
    };
    // Capture, before the focused terminal forwards the chord to its shell.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [focus]);

  // The keyboard coming into these panes makes them the ones ⌘K lists commands for.
  useEffect(() => {
    if (!enabled) return;
    if (focus.hasFocus()) setPaletteOwner(target.id);
    const unsubscribe = focus.subscribe({
      entered: () => setPaletteOwner(target.id),
      left: () => {},
      acted: () => setPaletteOwner(target.id),
    });
    return () => {
      unsubscribe();
      if (paletteOwner === target.id) setPaletteOwner(null);
    };
  }, [enabled, focus, target.id]);
  const listsCommands = usePaneCommandsListed(target.id, enabled, defaultForPalette);

  /** The pane ⌘K acts on as a command runs, or null once it's gone from the split. */
  const commandPane = (): string | null => {
    const key = target.read()?.focusedKey ?? singlePaneKey;
    return key !== null && isLive(key) ? key : null;
  };
  const commandPaneRef = useRef(commandPane);
  commandPaneRef.current = commandPane;
  const onCommandPane = (run: (paneKey: string) => void) => () => {
    const paneKey = commandPaneRef.current();
    if (paneKey === null) {
      showToast(PANE_CLOSED_MESSAGE);
      return;
    }
    run(paneKey);
  };
  const runRef = useRef({ onCommandPane });
  runRef.current = { onCommandPane };

  const moveSource = useMemo(
    () =>
      (paneKey: () => string | null): PaneMoveSource => ({
        read: () => {
          const key = paneKey();
          const current = target.read();
          return key === null || !current
            ? null
            : { layout: current.layout, zoomedKey: zoomedPaneKey(target), key };
        },
        sizing: () => focus.sizing(),
        nameOf: (key) => latest.current.paneName?.(key) ?? key,
        move: (destination) => {
          if (!isLive(destination.key)) return;
          moveSplitPane(target, destination, focus.sizing());
          focus.focus(destination.key);
        },
      }),
    [target, focus],
  );
  const moveAvailability = usePaneMoveAvailability({
    layout: isSplit ? split?.layout : null,
    zoomedKey,
    paneKey: targetKey,
    sizing: () => focus.sizing(),
    rootRef: panesRef,
  });

  const splitAvailability = availability(
    canSplit && !actions.splitting && !actions.closingSplit,
    !canSplit ? undefined : actions.closingSplit ? SPLIT_CLOSING_MESSAGE : SPLIT_PENDING_REASON,
  );
  const zoomLabel = zoomedKey === null ? PANE_ZOOM_LABELS.maximize : PANE_ZOOM_LABELS.restore;
  const signature = JSON.stringify([
    listsCommands,
    splitAvailability,
    isSplit,
    canClose,
    swapDirections,
    zoomLabel,
    moveAvailability,
    targetKey !== null,
  ]);
  const commands = useMemo<PaletteCommand[]>(() => {
    if (!listsCommands) return NO_COMMANDS;
    const keyCommand = (
      id: PaneShortcutId,
      icon: LucideIcon,
      keywords: string[],
      available: CommandAvailability,
      label = paneShortcutLabel(id),
    ): PaletteCommand => ({
      id: `pane:${target.id}:${id}`,
      label,
      icon,
      keywords: ["pane", "split", "terminal", ...keywords],
      nativeShortcut: paneShortcutChords(id)[0],
      availability: available,
      movesFocus: true,
      run: runRef.current.onCommandPane((paneKey) => void runPaneKeyRef.current(id, paneKey)),
    });
    const inSplit = availability(isSplit);
    const list: PaletteCommand[] = [
      keyCommand("terminal.splitRight", Columns2Icon, ["side by side", "shell"], splitAvailability),
      keyCommand("terminal.splitDown", Rows2Icon, ["stack", "shell"], splitAvailability),
      ...DIRECTIONS.map((direction) =>
        keyCommand(FOCUS_IDS[direction], FOCUS_ICONS[direction], ["focus", "move"], inSplit),
      ),
      ...DIRECTIONS.map((direction) =>
        keyCommand(RESIZE_IDS[direction], RESIZE_ICONS[direction], ["resize", "divider"], inSplit),
      ),
      keyCommand("terminal.equalize", EqualIcon, ["equalize", "balance", "even"], inSplit),
      keyCommand(
        "terminal.toggleZoom",
        zoomedKey === null ? Maximize2Icon : Minimize2Icon,
        ["zoom", "maximize", "restore", "fill"],
        inSplit,
        zoomLabel,
      ),
      ...DIRECTIONS.map((direction): PaletteCommand => ({
        id: `pane:${target.id}:${PANE_SWAP_IDS[direction]}`,
        label: paneSwapLabel(direction),
        icon: direction === "left" || direction === "right" ? ArrowLeftRightIcon : ArrowUpDownIcon,
        keywords: ["pane", "split", "swap", "rearrange", "exchange"],
        availability: availability(swapDirections.includes(direction)),
        movesFocus: true,
        run: runRef.current.onCommandPane((paneKey) => {
          if (swapSplitPane(target, paneKey, direction, focus.aspect()) !== null) {
            focus.focus(paneKey);
          }
        }),
      })),
      {
        id: `pane:${target.id}:close`,
        label: paneCommandLabel("close"),
        icon: XIcon,
        keywords: ["pane", "split", "close", "shell"],
        availability: availability(canClose),
        destructive: true,
        movesFocus: true,
        run: runRef.current.onCommandPane((paneKey) => {
          if (latest.current.actions.canClosePane(paneKey))
            latest.current.actions.closePane(paneKey);
        }),
      },
      {
        id: `pane:${target.id}:showToolbar`,
        label: "Show Pane Toolbar",
        icon: PanelTopIcon,
        keywords: ["pane", "toolbar", "buttons", "title", "keyboard"],
        availability: availability(targetKey !== null),
        movesFocus: true,
        run: runRef.current.onCommandPane((paneKey) => showPaneToolbar(panesRef.current, paneKey)),
      },
    ];
    if (moveAvailability !== null) {
      list.push(
        movePaneCommand(
          `pane:${target.id}:terminal.movePane`,
          moveSource(() => commandPaneRef.current()),
          moveAvailability,
        ),
      );
    }
    return list;
    // signature stands for the availability and labels the rows show.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, target, focus, moveSource, panesRef]);
  usePaletteCommands(`panes:${target.id}`, PANE_COMMANDS_HEADING, commands);

  // A pane's own commands for its row's menu, read as the row renders; each checks again that
  // its pane is still there as it runs.
  return (paneKey) => {
    if (!isSplit || !paneKeys.includes(paneKey)) return null;
    const current = target.read();
    const zoomedNow = zoomedPaneKey(target);
    const swaps: PaneMenuChoice[] = paneSwapDirections(current?.layout, zoomedNow, paneKey, 1).map(
      (direction) => ({
        id: PANE_SWAP_IDS[direction],
        label: paneSwapLabel(direction),
        command: PANE_SWAP_IDS[direction],
        run: () => {
          if (
            isLive(paneKey) &&
            swapSplitPane(target, paneKey, direction, focus.aspect()) !== null
          ) {
            focus.focus(paneKey);
          }
        },
      }),
    );
    const movable = paneMoveLayout(current?.layout, zoomedNow) !== null;
    return {
      zoomed: zoomedNow !== null,
      toggleZoom: () => {
        if (isLive(paneKey)) togglePaneZoom(target, focus, paneKey);
      },
      ...(swaps.length > 0 ? { swaps } : {}),
      ...(movable
        ? { moveOptions: () => (isLive(paneKey) ? paneMoveOptions(moveSource(() => paneKey)) : []) }
        : {}),
      ...(actions.canClosePane(paneKey)
        ? {
            close: () => {
              const live = latest.current.actions;
              if (!isLive(paneKey) || !live.canClosePane(paneKey)) return;
              focus.focus(paneKey);
              live.closePane(paneKey);
            },
          }
        : {}),
    };
  };
}
