import type { ReactNode } from "react";

export { CompactKbd, CompactShortcutKeys } from "@/components/ui/kbd";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";
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
export const QUIT_KEYS = [MOD_KEY, "Q"] as const;
export const COMMAND_PALETTE_KEYS = [MOD_KEY, "K"] as const;
export const SIDEBAR_TOGGLE_KEYS = [MOD_KEY, ALT_KEY, "["] as const;
export const RIGHT_SIDEBAR_TOGGLE_KEYS = [MOD_KEY, ALT_KEY, "]"] as const;
export const SETTINGS_KEYS = [MOD_KEY, ALT_KEY, ","] as const;

export function newSessionShortcutKeys(native = isNativeShell()): string[] {
  return native ? [MOD_KEY, "N"] : [MOD_KEY, IS_MAC ? SHIFT_KEY : "Shift", "O"];
}

export function newSessionAriaShortcut(native = isNativeShell()): string {
  return `${ARIA_MOD_KEY}+${native ? "N" : "Shift+O"}`;
}

/** Keys for the pinned-session jump chord; `digit` is one slot's digit or a range label. */
export function pinnedSessionShortcutKeys(digit: string, native = isNativeShell()): string[] {
  return native ? [MOD_KEY, digit] : [MOD_KEY, ALT_KEY, digit];
}

export function pinnedSessionAriaShortcut(digit: string, native = isNativeShell()): string {
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
