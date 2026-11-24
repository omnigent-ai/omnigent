// One tab in the tabs sidebar's strip: its icon, title, badge and close button,
// with the selected highlight and a keyboard-only focus ring around the whole tab.

import type { LucideIcon } from "lucide-react";
import { PlusIcon, XIcon } from "lucide-react";
import {
  type ComponentProps,
  type CSSProperties,
  Fragment,
  type KeyboardEvent,
  type ReactNode,
  useState,
} from "react";

import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { CompactShortcutKeys } from "@/components/KeyboardShortcut";
import { MENU_FILTER_TARGET_PROPS } from "@/components/ui/menu-filter";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { TabsTrigger, tabsTriggerClassName } from "@/components/ui/tabs";
import { useInertRef } from "@/hooks/useInertRef";
import { SuppressBrowserView } from "@/hooks/useSuppressBrowserView";
import { cn } from "@/lib/utils";
import {
  type ShortcutId,
  shortcutAriaKeys,
  shortcutKeys,
} from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { TAB_BADGE_BASE } from "@/shell/railTabs";
import { WorkspaceTabTooltip } from "@/shell/WorkspacePanel";
import { closeTabLabel } from "@/shell/workspaceCommands/model";
import { FileTypeIcon } from "@/shell/FileTypeIcon";
import { useStripEntryDrag } from "@/shell/workspaceSidebar/stripDrag";
import { useStripEntryMotion } from "@/shell/workspaceSidebar/useTabStripPresence";

import { clearPointerFocus, isKeyboardFocused, markPointerFocus } from "./stripTabFocus";
import type { TabId } from "./workspacePlace";

/**
 * A short, quiet line in the gap before each tab. It's absolutely placed so no tab's
 * width changes, and it hides next to the selected, hovered or keyboard-focused tab, by the
 * same keyboard-focus mark the focus ring draws on.
 */
const TAB_DIVIDER = cn(
  "relative before:pointer-events-none before:absolute before:top-1/2 before:left-[-1.5px] before:h-4 before:w-px before:-translate-y-1/2 before:bg-border before:transition-opacity before:content-['']",
  "first:before:opacity-0 data-selected:before:opacity-0 hover:before:opacity-0 data-keyboard-focus:before:opacity-0",
  "[[data-tab-strip-entry][data-selected]+&]:before:opacity-0 [[data-tab-strip-entry]:hover+&]:before:opacity-0 [[data-keyboard-focus]+&]:before:opacity-0",
  // A closing tab's ghost draws no line, so neither does the tab it is leaving beside.
  "[[data-tab-strip-leaving]+&]:before:opacity-0",
);

/** The trigger's own classes, which a closing tab's ghost copies so its title doesn't move. */
const TRIGGER_CLASS =
  "h-full min-w-0 flex-1 justify-start gap-1 border-0 bg-transparent! px-2 text-inherit shadow-none! outline-none! focus-visible:border-transparent focus-visible:ring-0 data-active:text-foreground";

function TabIcon({ title }: { title: TabTitle }) {
  if (title.connecting) return <Spinner className="size-3.5" />;
  if (title.filePath !== undefined) return <FileTypeIcon path={title.filePath} />;
  return <title.icon className="size-4 shrink-0" />;
}

export interface TabTitle {
  label: string;
  tooltip: string;
  icon: LucideIcon;
  /** A file tab's path; its file-type icon replaces `icon`. */
  filePath?: string;
  badge?: string;
  connecting: boolean;
}

/** A pinned tab's width: its icon only, as a browser's pinned tab. */
export const PINNED_TAB_WIDTH = 28;

export function StripTab({
  tabId,
  index,
  title,
  pinned = false,
  unseen = false,
  selected,
  closable,
  onClose,
  width,
  onKeyDown,
  menu,
  onMenuCloseAutoFocus,
  menuContentProps,
}: {
  tabId: TabId;
  index: number;
  title: TabTitle;
  /** Shown as its icon only, with its title for the tooltip and screen readers, and no ×. */
  pinned?: boolean;
  unseen?: boolean;
  selected: boolean;
  closable: boolean;
  /** `pointer` for the × or a middle click, so the strip can hold its tab widths. */
  onClose: (source: "pointer") => void;
  /** The width every tab in the strip shares, in pixels. */
  width: number;
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
  menu: ReactNode;
  /** Where focus goes as the menu closes, e.g. into the view an item it ran opened. */
  onMenuCloseAutoFocus?: (event: Event) => void;
  /** The menu content's own handlers, such as a filter field's focus, Escape and typed keys. */
  menuContentProps?: Pick<
    ComponentProps<typeof ContextMenuContent>,
    "onFocus" | "onEscapeKeyDown" | "onKeyDown"
  >;
}) {
  const motion = useStripEntryMotion();
  const leaving = motion.phase === "leaving";
  // On the same element the live tab had, so a closed tab's ghost shrinks from the width
  // it has on screen, even mid-grow or mid-reflow, and a reopened one grows from it again.
  const drag = useStripEntryDrag();
  const elementRef = useInertRef<HTMLDivElement>(leaving, drag?.setNodeRef);
  // Whether the trigger has keyboard focus, which the ring and the dividers both follow. A
  // trigger removed while focused may never blur, so a tab that starts leaving forgets it.
  const [keyboardFocused, setKeyboardFocused] = useState(false);
  if (leaving && keyboardFocused) setKeyboardFocused(false);
  const widthStyle = { "--tab-width": `${pinned ? PINNED_TAB_WIDTH : width}px` } as CSSProperties;
  const labelClass = pinned ? "sr-only" : "min-w-0 truncate";
  // An icon-only tab names itself, and says its count, as its title and pill would.
  const withCount = (text: string) =>
    pinned && title.badge !== undefined ? `${text} ${title.badge}` : text;
  const pinnedName = pinned ? withCount(title.label) : undefined;
  const triggerClass = cn(TRIGGER_CLASS, pinned && "justify-center px-0");
  // A closed tab's ghost: only a picture of the tab, out of focus order, the accessibility
  // tree and pointer hits from the moment the tab closed, as it shrinks away.
  const ghost = (
    <div
      ref={elementRef}
      data-tab-strip-leaving
      aria-hidden
      style={widthStyle}
      className="flex h-[24px] shrink-0 items-center overflow-hidden rounded-md text-muted-foreground pointer-events-none"
    >
      <span className={tabsTriggerClassName(triggerClass)}>
        <TabIcon title={title} />
        <span className={labelClass}>{title.label}</span>
      </span>
    </div>
  );
  const tab = leaving ? (
    ghost
  ) : (
    <div
      ref={elementRef}
      data-tab-strip-entry
      data-tab-pinned={pinned || undefined}
      data-tab-entering={motion.phase === "entering" || undefined}
      data-selected={selected || undefined}
      data-keyboard-focus={keyboardFocused || undefined}
      data-tab-dragging={drag?.dragging || undefined}
      {...drag?.listeners}
      style={{ ...widthStyle, ...drag?.style }}
      className={cn(
        // A dragged tab rides above the ones sliding aside, lifted like a picked-up tab.
        "data-tab-dragging:relative data-tab-dragging:z-10 data-tab-dragging:shadow-md",
        "group/tab flex h-[24px] w-(--tab-width) shrink-0 items-center gap-0.5 rounded-md pr-0.5 transition-colors",
        TAB_DIVIDER,
        // Keyboard focus rings the whole tab, × included, inside its edge so the scroller can't clip it.
        "data-keyboard-focus:ring-2 data-keyboard-focus:ring-ring data-keyboard-focus:ring-inset",
        // Forced colors drop box shadows, so the same focus shows as an inset system outline there.
        "forced-colors:data-keyboard-focus:outline-2 forced-colors:data-keyboard-focus:-outline-offset-2 forced-colors:data-keyboard-focus:outline-solid forced-colors:data-keyboard-focus:outline-[Highlight]",
        selected
          ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
          : "text-muted-foreground hover:bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] hover:text-foreground",
      )}
    >
      <TabsTrigger
        value={tabId}
        title={withCount(title.tooltip)}
        aria-label={pinnedName}
        data-workspace-tab-id={tabId}
        data-workspace-tab-index={index}
        // Pointer focus is marked so it doesn't count as keyboard focus; each event that can
        // change which it is samples the one predicate again.
        onPointerDown={(event) => {
          markPointerFocus(event.currentTarget);
          setKeyboardFocused(isKeyboardFocused(event.currentTarget));
        }}
        onFocus={(event) => setKeyboardFocused(isKeyboardFocused(event.currentTarget))}
        onBlur={(event) => {
          clearPointerFocus(event.currentTarget);
          setKeyboardFocused(false);
        }}
        onKeyDown={(event) => {
          clearPointerFocus(event.currentTarget);
          setKeyboardFocused(isKeyboardFocused(event.currentTarget));
          onKeyDown(event);
        }}
        onAuxClick={(event) => {
          if (event.button === 1 && closable) {
            event.preventDefault();
            onClose("pointer");
          }
        }}
        className={triggerClass}
      >
        <TabIcon title={title} />
        <span className={labelClass}>{title.label}</span>
        {title.badge !== undefined && !pinned && (
          <span className={cn(TAB_BADGE_BASE, "bg-muted text-muted-foreground")}>
            {title.badge}
          </span>
        )}
        {unseen && !selected && (
          <span aria-label="Updated" className="size-1.5 shrink-0 rounded-full bg-primary" />
        )}
      </TabsTrigger>
      {closable && !pinned && (
        <button
          type="button"
          tabIndex={-1}
          aria-label={closeTabLabel(title.label)}
          className={cn(
            "flex size-4 shrink-0 items-center justify-center rounded hover:bg-muted",
            // Quiet until the tab is selected or hovered, so a full strip reads as names.
            selected
              ? "opacity-70"
              : "opacity-0 group-hover/tab:opacity-70 focus-visible:opacity-100",
          )}
          onClick={() => onClose("pointer")}
        >
          <XIcon className="size-3" />
        </button>
      )}
    </div>
  );
  if (menu === null) return tab;
  // A ghost keeps the menu's wrapper, so React keeps the tab's element, but not the menu.
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={leaving}>
        {tab}
      </ContextMenuTrigger>
      {!leaving && (
        <ContextMenuContent {...menuContentProps} onCloseAutoFocus={onMenuCloseAutoFocus}>
          <SuppressBrowserView />
          {menu}
        </ContextMenuContent>
      )}
    </ContextMenu>
  );
}

/** One row of a strip tab's context menu; `shortcut` names the registry binding it shows. */
export interface StripTabMenuItem {
  id: string;
  label: string;
  disabled: boolean;
  shortcut?: ShortcutId;
  onSelect: () => void;
  separatorBefore?: boolean;
}

/** A strip tab's context menu rows, each with the chord the shortcut registry has for it. */
export function StripTabMenu({
  items,
  target,
}: {
  items: readonly StripTabMenuItem[];
  /** The item Enter in the menu's filter field runs, marked as if highlighted. */
  target?: string;
}) {
  return items.map((item) => (
    <Fragment key={item.id}>
      {item.separatorBefore && <ContextMenuSeparator />}
      <ContextMenuItem
        {...(item.id === target ? MENU_FILTER_TARGET_PROPS : {})}
        disabled={item.disabled}
        onSelect={item.onSelect}
      >
        {item.label}
        {item.shortcut && (
          <CompactShortcutKeys keys={shortcutKeys(item.shortcut)} className="ml-auto pl-4" />
        )}
      </ContextMenuItem>
    </Fragment>
  ));
}

/** The room the divider after "+" takes in the strip: the row's 4px gap before it, and its line. */
export const NEW_TAB_DIVIDER_ROOM = 4 + 1;

/** The strip's divider between its tabs and its controls, drawn like the tabs' own separators. */
export function StripDivider() {
  return <Separator orientation="vertical" className="h-4 data-vertical:self-center" />;
}

/**
 * The strip's "+", whose tooltip shows New tab's chord from the shortcut registry, and the
 * divider that sets it apart from the strip's controls, drawn like the tabs' own separators.
 */
export function NewTabButton({
  onClick,
  divider = true,
}: {
  onClick: () => void;
  /** Off where the strip draws its divider elsewhere: floating panes put it before All tabs. */
  divider?: boolean;
}) {
  return (
    <>
      <WorkspaceTabTooltip label="New tab" shortcut={shortcutKeys("workspace.newTab")}>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="size-6 shrink-0"
          aria-label="New tab"
          aria-keyshortcuts={shortcutAriaKeys("workspace.newTab")}
          onClick={onClick}
        >
          <PlusIcon className="size-4" />
        </Button>
      </WorkspaceTabTooltip>
      {divider ? <StripDivider /> : null}
    </>
  );
}
