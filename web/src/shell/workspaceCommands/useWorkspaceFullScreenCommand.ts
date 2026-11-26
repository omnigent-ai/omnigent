// The workspace's full-screen toggle: one action for ⇧⌘F, its ⌘K row and the panel's
// button. Bound once at the app shell, where the rail's open and maximized state lives,
// so the chord and the row work while the rail is closed too.

import { MaximizeIcon, MinimizeIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import type { WorkspaceFullScreenCommand } from "./model";

// The keyCode browsers report for a key an IME is still processing.
const IME_PROCESSING_KEY_CODE = 229;

interface FullScreenRuntime {
  command: (fullScreen: boolean) => WorkspaceFullScreenCommand | null;
  matchesChord: (event: globalThis.KeyboardEvent, mac: boolean) => boolean;
}

let runtime: Promise<FullScreenRuntime> | null = null;

// The labels live with the tabs sidebar's code, which the classic layout never loads; the
// chord is in the app's shortcut registry, which every page has.
function loadRuntime(): Promise<FullScreenRuntime> {
  runtime ??= import("./model").then(
    (model) => ({
      command: (fullScreen) => model.workspaceFullScreenCommand({ fullScreen }),
      matchesChord: (event, mac) => matchesShortcut(event, "workspace.toggleFullScreen", { mac }),
    }),
    (error: unknown) => {
      // A chunk that failed to load, such as after a deploy, is tried again on the next mount.
      runtime = null;
      throw error;
    },
  );
  return runtime;
}

export interface WorkspaceFullScreenOptions {
  /** The routed session, which scopes the ⌘K row; null offers nothing. */
  conversationId: string | null;
  /** Whether the workspace fills the window; null where it can't go full screen. */
  fullScreen: boolean | null;
  /** Enter or leave full screen, opening a closed rail on the way in. */
  toggle: () => void;
}

/** Registers the ⌘K row and binds the chord once the tabs sidebar can go full screen. */
export function useWorkspaceFullScreenCommand(
  { conversationId, fullScreen, toggle }: WorkspaceFullScreenOptions,
  isMac = isMacPlatform(),
): void {
  const toggleRef = useRef(toggle);
  toggleRef.current = toggle;
  const available = conversationId !== null && fullScreen !== null;
  const [loaded, setLoaded] = useState<FullScreenRuntime | null>(null);
  useEffect(() => {
    if (!available || loaded) return;
    let current = true;
    loadRuntime().then(
      (next) => {
        if (current) setLoaded(next);
      },
      (error: unknown) => console.warn("[workspace] full screen is unavailable:", error),
    );
    return () => {
      current = false;
    };
  }, [available, loaded]);
  const ready = available && loaded !== null ? loaded : null;
  const readyRef = useRef(ready);
  readyRef.current = ready;

  const command = ready && fullScreen !== null ? ready.command(fullScreen) : null;
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      command
        ? [
            {
              // The session is part of the id, so a row built for another session never runs.
              id: `workspace:${conversationId}:${command.id}`,
              label: command.label,
              icon: command.fullScreen ? MinimizeIcon : MaximizeIcon,
              keywords: [...command.keywords],
              availability: command.availability,
              shortcut: "workspace.toggleFullScreen",
              run: () => toggleRef.current(),
            },
          ]
        : [],
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the label follows fullScreen.
    [conversationId, command?.label],
  );
  usePaletteCommands(
    `workspace-full-screen:${conversationId ?? "none"}`,
    "Workspace",
    paletteCommands,
  );

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent): void => {
      const current = readyRef.current;
      if (!current) return;
      // A focused editor or field that handled the chord keeps it, and so does an IME.
      if (event.defaultPrevented || event.isComposing || event.keyCode === IME_PROCESSING_KEY_CODE)
        return;
      if (!current.matchesChord(event, isMac)) return;
      event.preventDefault();
      event.stopPropagation();
      // Holding the chord would flap the workspace in and out of full screen.
      if (event.repeat) return;
      toggleRef.current();
    };
    // Bubble phase, so the focused control sees the chord first.
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isMac]);
}
