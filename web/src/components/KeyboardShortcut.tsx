import type { ReactNode } from "react";
import { TooltipContent } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

const IS_MAC =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || "");

export const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
export const ALT_KEY = IS_MAC ? "⌥" : "Alt";
export const ENTER_KEY = "↵";
export const SHIFT_KEY = "⇧";

export function composerSendShortcutKeys(submitWithModEnter: boolean): string[] {
  return submitWithModEnter ? [MOD_KEY, ENTER_KEY] : [ENTER_KEY];
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

export function KeyboardShortcutHint({ label, keys }: { label: string; keys: string[] }) {
  return (
    <>
      <span>{label}</span>
      {keys.map((key) => (
        <Kbd key={`${label}-${key}`} variant="dark">
          {key}
        </Kbd>
      ))}
    </>
  );
}

export function KeyboardShortcutTooltipContent({ label, keys }: { label: string; keys: string[] }) {
  return (
    <TooltipContent
      side="top"
      className="border border-slate-700 bg-slate-900 text-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      <KeyboardShortcutHint label={label} keys={keys} />
    </TooltipContent>
  );
}
