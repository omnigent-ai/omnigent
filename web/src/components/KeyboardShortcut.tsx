import type { ReactNode } from "react";

export { CompactKbd, CompactShortcutKeys } from "@/components/ui/kbd";
import { TooltipContent } from "@/components/ui/tooltip";
import { isMacPlatform } from "@/lib/hotkeys";
import { cn } from "@/lib/utils";

const IS_MAC = isMacPlatform();

export const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
// Literal Control on every platform (⌃ on Mac), for chords that use Ctrl rather
// than the platform command modifier — unlike MOD_KEY, which is ⌘ on Mac.
export const CTRL_KEY = IS_MAC ? "⌃" : "Ctrl";
export const ALT_KEY = IS_MAC ? "⌥" : "Alt";
export const ENTER_KEY = "↵";
export const SHIFT_KEY = "⇧";
export const ARIA_MOD_KEY = IS_MAC ? "Meta" : "Control";
export const VIEW_MODE_TOGGLE_KEYS = [MOD_KEY, ALT_KEY, "\\"] as const;

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
    <TooltipContent
      side="top"
      shortcut={keys}
      className="border border-slate-700 bg-slate-900 text-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      <span>{label}</span>
    </TooltipContent>
  );
}
