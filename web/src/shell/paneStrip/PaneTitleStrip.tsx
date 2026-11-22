// A pane's strip row: its icon and title, and on the active or hovered pane the actions that fit,
// with the rest in its menu and ⌘K. A click runs the pane's own command and leaves the keyboard in
// its terminal; ⌘K's Show Pane Toolbar moves the keyboard into the row.

import {
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useRef,
} from "react";
import {
  ArrowRightToLineIcon,
  Columns2Icon,
  Maximize2Icon,
  MessageSquareIcon,
  Minimize2Icon,
  Rows2Icon,
  SquareTerminalIcon,
  XIcon,
} from "lucide-react";

import { KeyboardShortcutTooltip } from "@/components/KeyboardShortcut";
import { Button } from "@/components/ui/button";
import { Toolbar, ToolbarButton } from "@/components/ui/toolbar";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuSub,
  ContextMenuSubContent,
  ContextMenuSubTrigger,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { cn } from "@/lib/utils";

import { PANE_ZOOM_LABELS, paneCommandLabel } from "../paneCommandTable";
import { MOVE_PANE_LABEL, NO_ROOM_TO_MOVE_REASON } from "../paneMove";
import type { PaneMenuChoice } from "./paneMenu";
import { PANE_STRIP_CONTROL } from "./usePaneMoveDrag";
import { paneShortcutAria, paneShortcutKeys } from "./paneShortcutHints";
import { type PaneStripLayout, type PaneStripParts, STRIP_BUTTON_WIDTH } from "./paneStripLayout";
import {
  PANE_TOOLBAR_ATTRIBUTE,
  PANE_TOOLBAR_BUTTON_ATTRIBUTE,
  type PaneToolbarHandoff,
  returnFromPaneToolbar,
  runFromPaneToolbar,
} from "./paneToolbarFocus";

/** A pane's commands as its row offers them; each is the pane's own, shared with ⌘K and menus. */
export interface PaneStripCommands {
  splitRight?: () => void;
  splitDown?: () => void;
  /** Maximize or Restore this pane; absent while its split has one pane. */
  toggleZoom?: () => void;
  /** Close this pane; absent for a lone pane, the agent's, or where closing can't work. */
  close?: () => void;
  /** Put the pane back in the workspace sidebar: an app pane's tab, or a shell, still running. */
  moveToSidebar?: () => void;
  /** Swap with Pane toward each direction that has a pane, as ⌘K lists them: menu only. */
  swaps?: readonly PaneMenuChoice[];
  /** Move Pane's spots, as ⌘K's Move Pane… lists them, read as the menu's submenu opens. */
  moveOptions?: () => readonly PaneMenuChoice[];
}

/** What a pane kind shows in its row. */
export interface PaneStripPane {
  name: string;
  /** What Close says it closes, such as an app pane's tab; Close Pane by default. */
  closeLabel?: string;
  /** The row's icon, such as an app pane's tab icon; a terminal's by default. */
  icon?: ReactNode;
  /** Said after the name, such as the shell's session. */
  detail?: string;
  /** The name's full hover text, such as an app pane's tab tooltip; the name and detail by default. */
  tooltip?: string;
  /** Shown after the name, always, such as a shell's status. */
  status?: ReactNode;
  /** Returns from a shell the rail opened to the chat: the view's way back. */
  showChat?: () => void;
  commands: PaneStripCommands | null;
  /** Gives the pane's terminal the keyboard: a click on the row's blank space. */
  focus?: () => void;
  /** Gives the keyboard to the split's chosen pane without choosing: after a split, the new one. */
  refocus?: () => void;
  /** The session's chat pane, the one primary pane: its title reads a step stronger. */
  primary?: boolean;
  /** Where the chat pane's Chat/Terminal switch shows, at the row's end. */
  viewToggleHostRef?: (element: HTMLElement | null) => void;
}

/** The row's labels, as the shared pane command table and its zoom labels name them. */
export const PANE_STRIP_LABELS = Object.freeze({
  splitRight: paneCommandLabel("terminal.splitRight"),
  splitDown: paneCommandLabel("terminal.splitDown"),
  maximize: PANE_ZOOM_LABELS.maximize,
  restore: PANE_ZOOM_LABELS.restore,
  close: paneCommandLabel("close"),
  moveToSidebar: "Move to Sidebar",
  // A submenu, so no ellipsis: its spots show beside it.
  movePane: MOVE_PANE_LABEL.replace(/…$/, ""),
  showChat: "Show Chat",
});

/** The width a status takes in the row: its dot and a one-word label. */
export const STRIP_STATUS_WIDTH = 72;

/** The width the chat pane's Chat/Terminal switch takes in the row. */
export const STRIP_VIEW_TOGGLE_WIDTH = 74;

type ActionId = "splitRight" | "splitDown" | "maximize" | "moveToSidebar" | "close";

/**
 * The pane's actions in the order they stay as the row narrows; Close folds first. A maximized
 * pane shows no splits: a new pane wouldn't show until Restore.
 */
function availableActions(commands: PaneStripCommands | null, zoomed: boolean): ActionId[] {
  if (!commands) return [];
  return [
    ...(commands.splitRight && !zoomed ? (["splitRight"] as const) : []),
    ...(commands.splitDown && !zoomed ? (["splitDown"] as const) : []),
    ...(commands.toggleZoom && !zoomed ? (["maximize"] as const) : []),
    ...(commands.moveToSidebar ? (["moveToSidebar"] as const) : []),
    ...(commands.close ? (["close"] as const) : []),
  ];
}

/** The parts `pane`'s row lays out, for paneStripLayout. */
export function paneStripParts(
  pane: PaneStripPane,
  { zoomed }: { zoomed: boolean },
): PaneStripParts {
  return {
    actions: availableActions(pane.commands, zoomed).length,
    restore: zoomed && pane.commands?.toggleZoom !== undefined,
    fixed: [
      ...(pane.viewToggleHostRef ? [STRIP_VIEW_TOGGLE_WIDTH] : []),
      ...(pane.status ? [STRIP_STATUS_WIDTH] : []),
      ...(pane.showChat ? [STRIP_BUTTON_WIDTH] : []),
    ],
  };
}

/** The row's height: PANE_ROW_PX in paneGeometry. */
export const PANE_STRIP_HEIGHT_CLASS = "h-7";

// A click keeps the keyboard where it is, in the terminal; Enter or Space on a button acts too.
const keepFocus = (event: ReactMouseEvent) => event.preventDefault();

function StripButton({
  label,
  shortcut,
  onClick,
  pressed,
  children,
  testId,
}: {
  label: string;
  /** The pane command whose shortcut the tooltip shows. */
  shortcut?: string;
  onClick: (event: ReactMouseEvent<HTMLButtonElement>) => void;
  pressed?: boolean;
  children: ReactNode;
  testId: string;
}) {
  return (
    <KeyboardShortcutTooltip label={label} keys={paneShortcutKeys(shortcut)}>
      <ToolbarButton asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          aria-pressed={pressed}
          aria-keyshortcuts={paneShortcutAria(shortcut)}
          data-testid={testId}
          {...{ [PANE_TOOLBAR_BUTTON_ATTRIBUTE]: "" }}
          onMouseDown={keepFocus}
          onClick={onClick}
          // The row takes the terminal's colors, so hover uses its muted tone, not the app's accent.
          className={cn(
            "size-5 shrink-0 text-muted-foreground hover:bg-muted hover:text-foreground [&_svg]:size-3.5",
            pressed && "bg-muted text-foreground",
          )}
        >
          {children}
        </Button>
      </ToolbarButton>
    </KeyboardShortcutTooltip>
  );
}

export interface PaneTitleStripProps {
  paneKey: string;
  pane: PaneStripPane;
  layout: PaneStripLayout;
  /** The pane is active or the pointer is over it: its actions show. */
  actionsShown: boolean;
  zoomed: boolean;
  /** The split's focused pane: its toolbar carries the accent line, unless `focusLine` is off. */
  focused?: boolean;
  /** Off where the pane shows its focus another way, such as a card's brighter edge. */
  focusLine?: boolean;
  /**
   * The row starts at the window's top-left corner: on the Mac with the sidebar collapsed, the
   * traffic lights and the title-bar cluster sit at its start (index.css sizes the room).
   */
  windowCorner?: boolean;
  /**
   * The row starts under the Mac title bar's drag strip: it's as tall as the title bar, so it
   * lines up with the headers beside it, and a movable row drags its pane, not the window.
   */
  windowTop?: boolean;
  /**
   * The row is as tall as the title bar, as `windowTop` makes it, also while hidden behind a
   * maximized pane, so its pane keeps its size. Defaults to `windowTop`.
   */
  titleBarRow?: boolean;
  onFocusChange: (focused: boolean) => void;
  /**
   * Starts dragging the pane by the row's blank space, to move it beside another pane. Without
   * it the pane can't be dragged: a lone or maximized pane, or a shell that can't move panes.
   */
  onDragStart?: (event: ReactPointerEvent<HTMLDivElement>) => void;
  /** The pane is being dragged. */
  dragging?: boolean;
  /** The pane is held by its row, pressed or dragged: its handle shows, stretched in the accent. */
  held?: boolean;
}

export function PaneTitleStrip({
  paneKey,
  pane,
  layout,
  actionsShown,
  zoomed,
  focused = false,
  focusLine = true,
  windowCorner = false,
  windowTop = false,
  titleBarRow = windowTop,
  onFocusChange,
  onDragStart,
  dragging = false,
  held = false,
}: PaneTitleStripProps) {
  const movable = onDragStart !== undefined;
  // A click on the row's own space, not a control, is a click into the pane.
  const focusFromRow = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (onAControl(event)) return;
    // A right-click or Ctrl-click opens the row's menu: the keyboard stays where it is, for the
    // menu to hand back.
    if (event.button === 2 || (event.button === 0 && event.ctrlKey)) {
      event.preventDefault();
      return;
    }
    if (event.button !== 0 || !pane.focus) return;
    event.preventDefault();
    pane.focus();
  };
  // A double-click on the row's own space maximizes or restores the pane, as a title bar's does.
  const toggleZoomFromRow = (event: ReactMouseEvent<HTMLDivElement>) => {
    const toggleZoom = pane.commands?.toggleZoom;
    if (event.button !== 0 || !toggleZoom || onAControl(event)) return;
    event.preventDefault();
    toggleZoom();
    pane.focus?.();
  };
  return (
    <PaneStripMenu pane={pane} zoomed={zoomed}>
      <Toolbar asChild>
        <div
          aria-label={`${pane.name} pane`}
          data-pane-strip={paneKey}
          {...{ [PANE_TOOLBAR_ATTRIBUTE]: paneKey }}
          data-pane-strip-shown={actionsShown ? "" : undefined}
          data-pane-strip-compact={layout.compact ? "" : undefined}
          data-pane-strip-focused={focused ? "" : undefined}
          data-pane-strip-movable={movable ? "" : undefined}
          data-pane-strip-dragging={dragging ? "" : undefined}
          data-pane-strip-held={held ? "" : undefined}
          data-pane-strip-window-top={windowTop ? "" : undefined}
          data-pane-strip-title-bar-row={titleBarRow ? "" : undefined}
          data-pane-strip-zoomable={pane.commands?.toggleZoom ? "" : undefined}
          data-pane-strip-primary={pane.primary ? "" : undefined}
          className={cn(
            "group/pane-strip relative flex shrink-0 items-center gap-1 overflow-hidden bg-background px-1 text-xs",
            PANE_STRIP_HEIGHT_CLASS,
          )}
          // The row's blank space drags the pane; it keeps the normal pointer.
          onPointerDown={onDragStart}
          onMouseDown={focusFromRow}
          onDoubleClick={toggleZoomFromRow}
          onKeyDown={(event) => returnFromPaneToolbar(event, pane.focus)}
          onFocus={() => onFocusChange(true)}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null))
              onFocusChange(false);
          }}
        >
          {windowCorner ? (
            <span aria-hidden data-pane-strip-window-inset="" className="pane-strip-window-inset" />
          ) : null}
          {movable ? (
            // A pane that can move shows a small handle centred along its toolbar's top while the
            // pointer is over the row; the press goes through it to the row, which starts the drag.
            <span
              aria-hidden
              data-pane-strip-drag-handle=""
              className={cn(
                "pointer-events-none absolute top-1 left-1/2 -translate-x-1/2 rounded-full",
                "transition-[opacity,width,height,background-color] duration-150 ease-out",
                held
                  ? "h-1 w-12 bg-primary opacity-100"
                  : "h-[3px] w-7 bg-muted-foreground/60 opacity-0 group-hover/pane-strip:opacity-100",
              )}
            />
          ) : null}
          {focused && focusLine ? (
            // The split's focused pane: a 2 px accent line along its toolbar's top, drawn over the
            // session header's glass (z-30), which reaches down over the top row, and under the
            // surfaces that float above the page.
            <span
              aria-hidden
              data-pane-strip-focus-line=""
              className="pointer-events-none absolute inset-x-0 top-0 z-[31] h-0.5 bg-primary"
            />
          ) : null}
          <span
            aria-hidden
            className={cn(
              "mx-1 flex size-3.5 shrink-0 items-center justify-center text-muted-foreground [&_svg]:size-3.5",
              layout.compact && "hidden",
            )}
          >
            {pane.icon ?? <SquareTerminalIcon />}
          </span>
          <span
            className={cn(
              "min-w-0 flex-1 truncate text-foreground",
              pane.primary && "font-medium",
              (!layout.title || layout.compact) && "sr-only",
            )}
            data-testid="pane-strip-name"
            title={pane.tooltip ?? (pane.detail ? `${pane.name} · ${pane.detail}` : pane.name)}
          >
            {pane.name}
            {pane.detail ? <span className="text-muted-foreground"> · {pane.detail}</span> : null}
          </span>
          {!layout.title || layout.compact ? <span className="flex-1" /> : null}
          {pane.status ? <span className="flex shrink-0 items-center">{pane.status}</span> : null}
          {pane.showChat ? (
            <StripButton
              label={PANE_STRIP_LABELS.showChat}
              onClick={pane.showChat}
              testId="pane-strip-show-chat"
            >
              <MessageSquareIcon />
            </StripButton>
          ) : null}
          <PaneStripActions
            pane={pane}
            layout={layout}
            zoomed={zoomed}
            actionsShown={actionsShown}
          />
          {pane.viewToggleHostRef ? (
            // The chat pane's own switch between its chat and its terminal, at the row's end, so
            // the hover actions that show before it never move it.
            <span
              ref={pane.viewToggleHostRef}
              data-pane-strip-view-toggle-host=""
              className="flex shrink-0 items-center empty:hidden"
            />
          ) : null}
        </div>
      </Toolbar>
    </PaneStripMenu>
  );
}

/** Whether a row event happened on one of its controls, not its own space. */
const onAControl = (event: ReactMouseEvent) =>
  (event.target as Element).closest(PANE_STRIP_CONTROL) !== null;

/** A row menu item, with the shortcut of the pane command it runs when it has one. */
function PaneMenuItem({
  shortcut,
  onSelect,
  children,
}: {
  shortcut?: string;
  onSelect: (() => void) | undefined;
  children: ReactNode;
}) {
  const keys = paneShortcutKeys(shortcut);
  return (
    <ContextMenuItem onSelect={onSelect}>
      {children}
      {keys ? <ContextMenuShortcut>{keys.join("")}</ContextMenuShortcut> : null}
    </ContextMenuItem>
  );
}

const PANE_STRIP_MENU = "[data-pane-strip-menu]";

/** Whether the keyboard is nowhere, or still in the closing menu `menu`: only then is it ours. */
function keyboardLost(menu: EventTarget | null): boolean {
  const active = document.activeElement;
  if (active === null || active === document.body) return true;
  return menu instanceof Node && menu.contains(active);
}

/**
 * The row's right-click and Ctrl-click menu: the pane's actions as its buttons run them, with
 * their labels and shortcuts, whether or not the row has room to show them. A command records
 * the pane it leaves the keyboard in, and the menu gives it the keyboard as it closes.
 */
function PaneStripMenu({
  pane,
  zoomed,
  children,
}: {
  pane: PaneStripPane;
  zoomed: boolean;
  children: ReactNode;
}) {
  const { commands } = pane;
  const keyboardAfterClose = useRef<(() => void) | undefined>(undefined);
  // Opened from another pane's menu: where the keyboard was before is that menu's, now gone.
  const openedFromAMenu = useRef(false);
  // Move Pane's spots render apart from the menu, so a choice there leaves the keyboard in it.
  const moveSubmenu = useRef<HTMLDivElement>(null);
  const actions = availableActions(commands, zoomed);
  const restore = zoomed && commands?.toggleZoom;
  const swaps = commands?.swaps ?? [];
  const moveOptions = commands?.moveOptions;
  const rearranges = swaps.length > 0 || moveOptions !== undefined;
  // With no commands the row keeps the same parents, its menu disabled, so commands appearing
  // (a shell finishing its start) never remount it or what it measures.
  if (!commands || (actions.length === 0 && !restore && !rearranges)) {
    return (
      <ContextMenu modal={false}>
        <ContextMenuTrigger asChild disabled>
          {children}
        </ContextMenuTrigger>
        {null}
      </ContextMenu>
    );
  }
  const zoom = () => {
    keyboardAfterClose.current = pane.focus;
    commands.toggleZoom?.();
  };
  // A swap or move keeps the keyboard in the pane that moved.
  const rearrange = (choice: PaneMenuChoice) => () => {
    keyboardAfterClose.current = pane.focus;
    choice.run();
  };
  // Split from this pane, chosen first so the split's new pane takes over from it.
  const split = (command: (() => void) | undefined) => () => {
    keyboardAfterClose.current = pane.refocus ?? pane.focus;
    pane.focus?.();
    command?.();
  };
  const item = (id: ActionId) => {
    switch (id) {
      case "splitRight":
        return (
          <PaneMenuItem
            key={id}
            shortcut="terminal.splitRight"
            onSelect={split(commands.splitRight)}
          >
            {PANE_STRIP_LABELS.splitRight}
          </PaneMenuItem>
        );
      case "splitDown":
        return (
          <PaneMenuItem key={id} shortcut="terminal.splitDown" onSelect={split(commands.splitDown)}>
            {PANE_STRIP_LABELS.splitDown}
          </PaneMenuItem>
        );
      case "maximize":
        return (
          <PaneMenuItem key={id} shortcut="terminal.toggleZoom" onSelect={zoom}>
            {PANE_STRIP_LABELS.maximize}
          </PaneMenuItem>
        );
      case "moveToSidebar":
        return (
          <ContextMenuItem key={id} onSelect={commands.moveToSidebar}>
            {PANE_STRIP_LABELS.moveToSidebar}
          </ContextMenuItem>
        );
      case "close":
        return (
          <PaneMenuItem key={id} shortcut="terminal.closePane" onSelect={commands.close}>
            {pane.closeLabel ?? PANE_STRIP_LABELS.close}
          </PaneMenuItem>
        );
    }
  };
  const closing = actions.includes("close");
  const sizing = actions.filter((id) => id !== "close" && id !== "moveToSidebar");
  const moving = actions.includes("moveToSidebar") || rearranges;
  return (
    // Not modal: a right-click on another pane's toolbar opens that pane's menu at once.
    <ContextMenu
      modal={false}
      onOpenChange={(open) => {
        if (open) openedFromAMenu.current = !!document.activeElement?.closest(PANE_STRIP_MENU);
      }}
    >
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent
        data-pane-strip-menu=""
        onCloseAutoFocus={(event) => {
          const giveKeyboard =
            keyboardAfterClose.current ?? (openedFromAMenu.current ? pane.focus : undefined);
          keyboardAfterClose.current = undefined;
          // Focus somewhere new, such as another menu, the composer or a pane, stays there.
          if (!keyboardLost(event.target) && !keyboardLost(moveSubmenu.current)) {
            event.preventDefault();
          } else if (giveKeyboard) {
            event.preventDefault();
            giveKeyboard();
          }
        }}
      >
        {restore ? (
          <PaneMenuItem shortcut="terminal.toggleZoom" onSelect={zoom}>
            {PANE_STRIP_LABELS.restore}
          </PaneMenuItem>
        ) : null}
        {sizing.map(item)}
        {moving && (sizing.length > 0 || restore) ? <ContextMenuSeparator /> : null}
        {moveOptions ? (
          <ContextMenuSub>
            <ContextMenuSubTrigger>{PANE_STRIP_LABELS.movePane}</ContextMenuSubTrigger>
            <ContextMenuSubContent ref={moveSubmenu} data-pane-strip-move-menu="">
              <PaneMoveMenuItems options={moveOptions} choose={rearrange} />
            </ContextMenuSubContent>
          </ContextMenuSub>
        ) : null}
        {swaps.map((choice) => (
          <PaneMenuItem key={choice.id} shortcut={choice.command} onSelect={rearrange(choice)}>
            {choice.label}
          </PaneMenuItem>
        ))}
        {actions.includes("moveToSidebar") ? item("moveToSidebar") : null}
        {closing && (sizing.length > 0 || moving || restore) ? <ContextMenuSeparator /> : null}
        {closing ? item("close") : null}
      </ContextMenuContent>
    </ContextMenu>
  );
}

/** Move Pane's spots, read as its submenu opens; greyed with why when no spot has room. */
function PaneMoveMenuItems({
  options,
  choose,
}: {
  options: () => readonly PaneMenuChoice[];
  choose: (choice: PaneMenuChoice) => () => void;
}) {
  const spots = options();
  if (spots.length === 0)
    return <ContextMenuItem disabled>{NO_ROOM_TO_MOVE_REASON}</ContextMenuItem>;
  return spots.map((spot) => (
    <ContextMenuItem key={spot.id} onSelect={choose(spot)}>
      {spot.label}
    </ContextMenuItem>
  ));
}

/**
 * A pane's action buttons: Restore while maximized, then on the active or hovered pane its Split
 * Right, Split Down, Maximize and Close as the row has room.
 */
function PaneStripActions({
  pane,
  layout,
  zoomed,
  actionsShown,
}: {
  pane: PaneStripPane;
  layout: PaneStripLayout;
  zoomed: boolean;
  actionsShown: boolean;
}) {
  const { commands } = pane;
  const shownActions = actionsShown
    ? availableActions(commands, zoomed).slice(0, layout.actions)
    : [];
  // Pressed from the keyboard, a button hands its pane the keyboard as the row's menu does.
  const run =
    (command: (() => void) | undefined, handoff: PaneToolbarHandoff) =>
    (event: ReactMouseEvent<HTMLButtonElement>) =>
      runFromPaneToolbar(event.currentTarget, () => command?.(), pane.focus, handoff);
  const action = (id: ActionId) => {
    if (!commands) return null;
    switch (id) {
      case "splitRight":
        return (
          <StripButton
            key={id}
            label={PANE_STRIP_LABELS.splitRight}
            shortcut="terminal.splitRight"
            onClick={run(commands.splitRight, "before")}
            testId="pane-strip-split-right"
          >
            <Columns2Icon />
          </StripButton>
        );
      case "splitDown":
        return (
          <StripButton
            key={id}
            label={PANE_STRIP_LABELS.splitDown}
            shortcut="terminal.splitDown"
            onClick={run(commands.splitDown, "before")}
            testId="pane-strip-split-down"
          >
            <Rows2Icon />
          </StripButton>
        );
      case "maximize":
        return (
          <StripButton
            key={id}
            label={PANE_STRIP_LABELS.maximize}
            shortcut="terminal.toggleZoom"
            onClick={run(commands.toggleZoom, "after")}
            testId="pane-strip-maximize"
          >
            <Maximize2Icon />
          </StripButton>
        );
      case "moveToSidebar":
        return (
          <StripButton
            key={id}
            label={PANE_STRIP_LABELS.moveToSidebar}
            onClick={run(commands.moveToSidebar, "before")}
            testId="pane-strip-move-to-sidebar"
          >
            <ArrowRightToLineIcon />
          </StripButton>
        );
      case "close":
        return (
          <StripButton
            key={id}
            label={pane.closeLabel ?? PANE_STRIP_LABELS.close}
            shortcut="terminal.closePane"
            onClick={run(commands.close, "before")}
            testId="pane-strip-close"
          >
            <XIcon />
          </StripButton>
        );
    }
  };
  return (
    <>
      {/* Restore takes Maximize's place, before Close. */}
      {zoomed && commands?.toggleZoom ? (
        <StripButton
          label={PANE_STRIP_LABELS.restore}
          shortcut="terminal.toggleZoom"
          onClick={run(commands.toggleZoom, "after")}
          pressed
          testId="pane-strip-restore"
        >
          <Minimize2Icon />
        </StripButton>
      ) : null}
      {shownActions.map(action)}
    </>
  );
}
