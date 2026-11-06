import type { ReactNode } from "react";

export { CompactKbd, CompactShortcutKeys } from "@/components/ui/kbd";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell, supportsDesktopCommands } from "@/lib/nativeBridge";
import type { TerminalSplitAxis } from "@/lib/terminalSplitLayout";
import { cn } from "@/lib/utils";

const IS_MAC = isMacPlatform();

export const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
// Literal Control on every platform (⌃ on Mac), for chords that use Ctrl rather
// than the platform command modifier — unlike MOD_KEY, which is ⌘ on Mac.
export const CTRL_KEY = IS_MAC ? "⌃" : "Ctrl";
export const ALT_KEY = IS_MAC ? "⌥" : "Alt";
export const ENTER_KEY = "↵";
export const ESC_KEY = "Esc";
export const SHIFT_KEY = "⇧";
export const ARROW_UP_KEY = "↑";
export const ARROW_DOWN_KEY = "↓";
export const ARROW_LEFT_KEY = "←";
export const ARROW_RIGHT_KEY = "→";
export const ARIA_MOD_KEY = IS_MAC ? "Meta" : "Control";
export const VIEW_MODE_TOGGLE_KEYS = [MOD_KEY, ALT_KEY, "\\"] as const;
export const SPLIT_TERMINAL_KEYS: Record<TerminalSplitAxis, string[]> = {
  columns: [MOD_KEY, ALT_KEY, SHIFT_KEY, "\\"],
  rows: [MOD_KEY, ALT_KEY, "-"],
};
// ⌃` opens a shell in every runtime; the rest are desktop menu chords (see the
// accelerators in web/electron/src/main.js).
export const NEW_TERMINAL_KEYS = [CTRL_KEY, "`"] as const;
export const NEW_BROWSER_TAB_KEYS = [MOD_KEY, "T"] as const;
export const CLOSE_KEYS = [MOD_KEY, "W"] as const;
export const CLOSE_WINDOW_KEYS = [MOD_KEY, SHIFT_KEY, "W"] as const;
export const PIN_SESSION_KEYS = [MOD_KEY, ALT_KEY, "P"] as const;
export const ARCHIVE_SESSION_KEYS = [MOD_KEY, SHIFT_KEY, "A"] as const;
export const FORK_SESSION_KEYS = [MOD_KEY, ALT_KEY, "F"] as const;
export const QUIT_KEYS = [MOD_KEY, "Q"] as const;
export const RELOAD_KEYS = [MOD_KEY, "R"] as const;
export const ADDRESS_BAR_KEYS = [MOD_KEY, "L"] as const;
export const REOPEN_TAB_KEYS = [MOD_KEY, SHIFT_KEY, "T"] as const;
export const PREVIOUS_TAB_KEYS = [MOD_KEY, SHIFT_KEY, "["] as const;
export const NEXT_TAB_KEYS = [MOD_KEY, SHIFT_KEY, "]"] as const;
export const COMMAND_PALETTE_KEYS = [MOD_KEY, "K"] as const;
export const SIDEBAR_TOGGLE_KEYS = [MOD_KEY, ALT_KEY, "["] as const;
export const RIGHT_SIDEBAR_TOGGLE_KEYS = [MOD_KEY, ALT_KEY, "]"] as const;
export const SETTINGS_KEYS = [MOD_KEY, ALT_KEY, ","] as const;

/** A chord as plain text for native `title` tooltips: "⌘R" on macOS, "Ctrl+R" elsewhere. */
export function shortcutText(keys: readonly string[]): string {
  return keys.join(IS_MAC ? "" : "+");
}

export function newSessionShortcutKeys(native = isNativeShell()): string[] {
  return native ? [MOD_KEY, "N"] : [MOD_KEY, IS_MAC ? SHIFT_KEY : "Shift", "O"];
}

export function newSessionAriaShortcut(native = isNativeShell()): string {
  return `${ARIA_MOD_KEY}+${native ? "N" : "Shift+O"}`;
}

/**
 * Keys that open Settings: the desktop menu's ⌘, (Ctrl+, elsewhere), or ⌘⌥, in a
 * browser, where ⌘, belongs to the browser's own settings.
 */
export function settingsShortcutKeys(desktopMenu = supportsDesktopCommands()): string[] {
  return desktopMenu ? [MOD_KEY, ","] : [...SETTINGS_KEYS];
}

export function settingsAriaShortcut(desktopMenu = supportsDesktopCommands()): string {
  return `${ARIA_MOD_KEY}+${desktopMenu ? "," : "Alt+,"}`;
}

/**
 * Keys that toggle the left (Conversations) sidebar: ⌘B in the desktop app, or
 * ⌘⌥[ in a browser, where ⌘B can open the browser's bookmarks.
 */
export function sidebarToggleShortcutKeys(native = isNativeShell()): string[] {
  return native ? [MOD_KEY, "B"] : [...SIDEBAR_TOGGLE_KEYS];
}

export function sidebarToggleAriaShortcut(native = isNativeShell()): string {
  return `${ARIA_MOD_KEY}+${native ? "B" : "Alt+["}`;
}

/** Keys that toggle the right (Workspace) sidebar: ⇧⌘B in the desktop app, ⌘⌥] in a browser. */
export function rightSidebarToggleShortcutKeys(native = isNativeShell()): string[] {
  return native ? [MOD_KEY, SHIFT_KEY, "B"] : [...RIGHT_SIDEBAR_TOGGLE_KEYS];
}

export function rightSidebarToggleAriaShortcut(native = isNativeShell()): string {
  return `${ARIA_MOD_KEY}+${native ? "Shift+B" : "Alt+]"}`;
}

// Ctrl+digit opens sidebar sessions off macOS, so workspace tabs take Alt there.
const WORKSPACE_TAB_MODIFIER = IS_MAC ? CTRL_KEY : ALT_KEY;

/** Keys that pick a workspace tab by its position: ⌃1 on macOS, Alt+1 elsewhere. */
export function workspaceTabShortcutKeys(digit: string): string[] {
  return [WORKSPACE_TAB_MODIFIER, digit];
}

export function workspaceTabAriaShortcut(digit: string): string {
  return `${IS_MAC ? "Control" : "Alt"}+${digit}`;
}

/** Keys for the numbered-session jump chord; `digit` is one slot's digit or a range label. */
export function sessionNumberShortcutKeys(digit: string, native = isNativeShell()): string[] {
  return native ? [MOD_KEY, digit] : [MOD_KEY, ALT_KEY, digit];
}

export function sessionNumberAriaShortcut(digit: string, native = isNativeShell()): string {
  return native ? `${ARIA_MOD_KEY}+${digit}` : `${ARIA_MOD_KEY}+Alt+${digit}`;
}

export function composerSendShortcutKeys(submitWithModEnter: boolean): string[] {
  return submitWithModEnter ? [MOD_KEY, ENTER_KEY] : [ENTER_KEY];
}

export function composerSteerAllShortcutKeys(submitWithModEnter: boolean): string[] {
  return submitWithModEnter ? [MOD_KEY, SHIFT_KEY, ENTER_KEY] : [MOD_KEY, ENTER_KEY];
}

export function composerNewLineShortcutKeys(submitWithModEnter: boolean): string[] {
  return submitWithModEnter ? [ENTER_KEY] : [SHIFT_KEY, ENTER_KEY];
}

export function Kbd({
  children,
  variant = "default",
}: {
  children: ReactNode;
  variant?: "default" | "dark";
}) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "inline-flex h-6 min-w-6 items-center justify-center rounded-md border border-border bg-muted px-1.5 font-sans text-sm font-medium text-muted-foreground",
        variant === "dark" && "border-slate-600 bg-slate-700 text-slate-300",
      )}
    >
      {children}
    </kbd>
  );
}

export function KeyboardShortcutTooltipContent({ label, keys }: { label: string; keys: string[] }) {
  return (
    <TooltipContent side="top" shortcut={keys}>
      <span>{label}</span>
    </TooltipContent>
  );
}

/**
 * Hover and focus tooltip for one trigger: the action name, plus its chord
 * when `keys` is given. Omit `keys` when the chord is inactive in this state.
 */
export function KeyboardShortcutTooltip({
  label,
  keys,
  side = "top",
  children,
}: {
  label: string;
  keys?: readonly string[];
  side?: "top" | "right" | "bottom" | "left";
  children: ReactNode;
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side={side} shortcut={keys}>
          {label}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
