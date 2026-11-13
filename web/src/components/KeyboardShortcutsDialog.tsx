// A read-only "Keyboard shortcuts" overlay listing the shortcuts that already
// exist in the chat surface. The rows come from `@/lib/shortcutCatalog`, which
// mirrors the live behavior. Nothing here binds new behavior except the
// dialog's own opener (⌘/Ctrl + /), which this component registers.
//
// Self-contained: it owns its open state and listens for its opener directly
// (a window keydown for ⌘/Ctrl+/, plus a custom event so a menu entry can open
// it without prop-drilling). Mount it once near the app shell.

import { useEffect, useState } from "react";

import { Kbd } from "@/components/KeyboardShortcut";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { readSubmitWithModEnter } from "@/lib/composerSendShortcutPreferences";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { hasCommandModifier } from "@/lib/hotkeys";
import {
  isElectronShell,
  isNativeShell,
  supportsBrowser,
  supportsDesktopCommands,
} from "@/lib/nativeBridge";
import { shortcutGroupsFor } from "@/lib/shortcutCatalog";

// Custom event the dialog listens for, so non-adjacent surfaces (e.g. the
// account menu) can open it without threading state through the tree.
export const KEYBOARD_SHORTCUTS_EVENT = "omnigent:open-keyboard-shortcuts";

/** Dispatch the open event — used by menu entries that can't reach the state. */
export function openKeyboardShortcuts(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(KEYBOARD_SHORTCUTS_EVENT));
}

/**
 * The shortcut reference shared by the dialog and Settings page. The dialog
 * keeps the compact inline list; Settings uses section headings with bordered
 * list cards to match the rest of its content.
 */
export function KeyboardShortcutsList({
  variant = "compact",
}: {
  variant?: "compact" | "settings";
}) {
  // Feature-based, stable per session; computed at render so tests can vary it.
  const isMobileViewport = useIsMobileViewport();
  const isCoarsePointer = useIsCoarsePointer();
  const preventsKeyboardSubmit = isMobileViewport || isCoarsePointer;
  const serverInfo = useServerInfo();
  const groups = shortcutGroupsFor({
    native: isNativeShell(),
    electron: isElectronShell(),
    browser: supportsBrowser(),
    submitWithModEnter: readSubmitWithModEnter(),
    preventsKeyboardSubmit,
    contentSearch: !isMobileViewport && isFeatureEnabled(serverInfo, "workspace_content_search"),
    grids: !isMobileViewport && isFeatureEnabled(serverInfo, "grid_sessions_display_ui"),
    splitTerminal: !isMobileViewport && isFeatureEnabled(serverInfo, "splittable_terminal_view_ui"),
    desktopCommands: supportsDesktopCommands(),
    siblingSessions: serverInfo !== "loading" && serverInfo.sibling_sessions === true,
  });
  const settings = variant === "settings";
  return (
    <div className={settings ? "flex flex-col gap-10" : undefined}>
      {groups.map((group) => (
        <section key={group.title} className={settings ? "" : "mb-4 last:mb-0"}>
          <h3
            className={
              settings
                ? "mb-3 text-ui font-medium text-foreground"
                : "mb-1 text-sm font-medium text-muted-foreground"
            }
          >
            {group.title}
            {group.note ? (
              <span className="ml-1.5 font-normal text-muted-foreground/70">· {group.note}</span>
            ) : null}
          </h3>
          <ul className={settings ? "rounded-xl border border-border bg-card px-4" : undefined}>
            {group.items.map((item) => (
              <li
                key={item.label}
                className={
                  settings
                    ? "flex items-center justify-between gap-4 border-b border-border py-4 last:border-b-0"
                    : "flex items-center justify-between gap-4 border-b border-border/60 py-2.5 last:border-b-0"
                }
              >
                <span className="text-ui text-foreground">{item.label}</span>
                <span className="flex shrink-0 items-center gap-1">
                  {item.keys.map((key) => (
                    <Kbd key={`${item.label}-${key}`}>{key}</Kbd>
                  ))}
                  {item.alternateKeys ? (
                    <>
                      <span className="px-0.5 text-sm text-muted-foreground/70">or</span>
                      {item.alternateKeys.map((key) => (
                        <Kbd key={`${item.label}-alternate-${key}`}>{key}</Kbd>
                      ))}
                    </>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export function KeyboardShortcutsDialog() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      // ⌘/Ctrl + / toggles the panel. Plain `/` is the composer's slash-menu
      // trigger, so require the platform command modifier and no Shift/Alt to
      // avoid clashing (only ⌘/ on macOS, only Ctrl+/ on Win/Linux).
      if (hasCommandModifier(e) && !e.altKey && !e.shiftKey && e.key === "/") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    const onOpenEvent = () => setOpen(true);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener(KEYBOARD_SHORTCUTS_EVENT, onOpenEvent);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener(KEYBOARD_SHORTCUTS_EVENT, onOpenEvent);
    };
  }, []);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription className="sr-only">
            The keyboard shortcuts available in the chat.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[70vh] overflow-y-auto pr-1">
          <KeyboardShortcutsList />
        </div>
      </DialogContent>
    </Dialog>
  );
}
