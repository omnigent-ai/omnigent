// Filtering the Files tree by name: the ⌘K row that opens the tree's filter, and the
// requests it posts, which the session's tree takes up once it's on screen.

import { ListFilterIcon } from "lucide-react";
import { useEffect, useRef } from "react";
import { type PaletteCommand, AVAILABLE } from "@/store/paletteCommands";

/** The tree header's button and the ⌘K row. */
export const FILTER_TREE_BY_NAME_LABEL = "Filter Files Tree by Name";
/** Ends a name filter, from the filter's strip and ⌘K. */
export const CLEAR_TREE_FILTER_LABEL = "Clear Files Tree Filter";

/** Sessions whose tree was asked to open its filter, before one took it up. */
const pendingRequests = new Set<string>();
const listeners = new Set<(conversationId: string) => void>();

/** Asks the session's Files tree to open its name filter: now if on screen, else once it is. */
export function requestTreeNameFilter(conversationId: string): void {
  pendingRequests.add(conversationId);
  listeners.forEach((listener) => listener(conversationId));
}

/** Forgets requests no tree took up; for tests. */
export function forgetTreeNameFilterRequestsForTests(): void {
  pendingRequests.clear();
}

/**
 * Calls `onRequest` for each request in `conversationId`, including one posted before
 * this tree mounted. Pass no `conversationId` while the tree is off screen.
 */
export function useTreeNameFilterRequests(
  conversationId: string | undefined,
  onRequest: () => void,
): void {
  const latest = useRef(onRequest);
  latest.current = onRequest;
  useEffect(() => {
    if (!conversationId) return;
    const take = () => {
      if (!pendingRequests.delete(conversationId)) return;
      latest.current();
    };
    take();
    const listener = (requested: string) => {
      if (requested === conversationId) take();
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, [conversationId]);
}

/** The ⌘K row; `run` shows the tree and asks it to open its filter. */
export function filterTreeByNamePaletteCommand(run: () => void): PaletteCommand {
  return {
    id: "files.filter-by-name",
    label: FILTER_TREE_BY_NAME_LABEL,
    icon: ListFilterIcon,
    keywords: ["files", "tree", "filter", "name", "find", "narrow", "glob", "include", "exclude"],
    // A hidden tree may open as a popover, which the palette's closing focus would dismiss.
    movesFocus: true,
    availability: AVAILABLE,
    run,
  };
}
