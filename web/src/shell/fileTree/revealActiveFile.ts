// Reveal Active File: what the Files tree header's focus button and its ⌘K row do
// for the viewed file, and when they can. Both entry points post one request, which
// the session's Files panel takes up once it's on screen.

import { LocateFixedIcon } from "lucide-react";
import { useEffect, useRef } from "react";
import {
  type CommandAvailability,
  type PaletteCommand,
  AVAILABLE,
  unavailableBecause,
} from "@/store/paletteCommands";
import { activeFileTreePath } from "./activeFileTreePath";

/** The header button's name, beside the tree it acts on. */
export const REVEAL_ACTIVE_FILE_LABEL = "Reveal Active File";
/** The same command's ⌘K name, which says where it reveals the file. */
export const REVEAL_ACTIVE_FILE_PALETTE_LABEL = "Reveal Active File in Files Tree";
export const NO_ACTIVE_FILE_REASON = "Open a file to reveal it in the Files tree.";
export const OUTSIDE_WORKSPACE_REASON = "The open file is outside this workspace’s Files tree.";
export const HIDDEN_FILE_REASON = "The open file is hidden. Show hidden files to reveal it.";

export type ActiveFileRevealPlan =
  | { kind: "unavailable"; availability: CommandAvailability }
  /** `rerootToWorkspace`: the browsed folder doesn't hold the file, the workspace does. */
  | { kind: "reveal"; rerootToWorkspace: boolean; availability: CommandAvailability };

/** A path with a segment the tree hides while hidden files are off. */
function isHiddenPath(path: string): boolean {
  return path.split("/").some((part) => part.startsWith("."));
}

/**
 * How to reveal `activePath` in a tree browsing `location` (the panel's wire form:
 * "" for the workspace root): in place, or from the workspace root when the
 * browsed folder doesn't hold it. A file the tree can't show isn't offered.
 */
export function planActiveFileReveal({
  activePath,
  location,
  workspaceRoot,
  showHidden,
}: {
  activePath: string | null | undefined;
  location: string;
  workspaceRoot: string | null;
  showHidden: boolean;
}): ActiveFileRevealPlan {
  if (!activePath) {
    return { kind: "unavailable", availability: unavailableBecause(NO_ACTIVE_FILE_REASON) };
  }
  const inPlace = activeFileTreePath(activePath, location, workspaceRoot);
  const fromWorkspace = inPlace === null ? activeFileTreePath(activePath, "", workspaceRoot) : null;
  const treePath = inPlace ?? fromWorkspace;
  if (treePath === null) {
    return { kind: "unavailable", availability: unavailableBecause(OUTSIDE_WORKSPACE_REASON) };
  }
  if (!showHidden && isHiddenPath(treePath)) {
    return { kind: "unavailable", availability: unavailableBecause(HIDDEN_FILE_REASON) };
  }
  return { kind: "reveal", rerootToWorkspace: inPlace === null, availability: AVAILABLE };
}

/** The ⌘K row, the same wherever it's registered; `run` posts the reveal request. */
export function revealActiveFilePaletteCommand(
  availability: CommandAvailability,
  run: () => void,
): PaletteCommand {
  return {
    id: "files.reveal-active-file",
    label: REVEAL_ACTIVE_FILE_PALETTE_LABEL,
    icon: LocateFixedIcon,
    keywords: ["files", "tree", "reveal", "locate", "focus", "current", "open file"],
    // A hidden tree may open as a popover, which the palette's closing focus would dismiss.
    movesFocus: true,
    availability,
    run,
  };
}

/** Reveals asked for that no Files panel has taken up yet: each session's file to reveal. */
const pendingReveals = new Map<string, string | null>();
const revealListeners = new Set<(conversationId: string) => void>();

/**
 * Asks the session's Files panel to reveal `file`, the one viewed when it was asked:
 * now if the panel's on screen, else once it is. Showing the tree may close the file,
 * as the classic rail does, so the request keeps it.
 */
export function requestActiveFileReveal(conversationId: string, file: string | null): void {
  pendingReveals.set(conversationId, file);
  revealListeners.forEach((listener) => listener(conversationId));
}

/** Forgets requests no panel took up; for tests. */
export function forgetActiveFileRevealsForTests(): void {
  pendingReveals.clear();
}

/**
 * Calls `onRequest` for each reveal asked for in `conversationId`, including one
 * asked for before this panel mounted, as when ⌘K shows a hidden tree to reveal.
 * Pass no `conversationId` while the panel is off screen, so the one on screen takes it.
 */
export function useActiveFileRevealRequests(
  conversationId: string | undefined,
  onRequest: (file: string | null) => void,
): void {
  const latest = useRef(onRequest);
  latest.current = onRequest;
  useEffect(() => {
    if (!conversationId) return;
    const take = () => {
      if (!pendingReveals.has(conversationId)) return;
      const file = pendingReveals.get(conversationId) ?? null;
      pendingReveals.delete(conversationId);
      latest.current(file);
    };
    take();
    const listener = (requested: string) => {
      if (requested === conversationId) take();
    };
    revealListeners.add(listener);
    return () => {
      revealListeners.delete(listener);
    };
  }, [conversationId]);
}
