// The sidebar publishes its project order here while it's mounted, so ⌘K can
// move the project in view with the same facts and the same move as the folder menu.

import { useEffect } from "react";
import { create } from "zustand";

import type { ProjectMoveDestination, ProjectOrderingFacts } from "./ordering";

export interface ProjectOrdering {
  /** Project names in their current order. */
  names: readonly string[];
  facts: ProjectOrderingFacts;
  move: (name: string, destination: ProjectMoveDestination) => void;
}

export const projectOrderingStore = create<{ ordering: ProjectOrdering | null }>(() => ({
  ordering: null,
}));

/** Publish `ordering` while mounted and `enabled`; the project list owns it. */
export function usePublishProjectOrdering(ordering: ProjectOrdering, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    projectOrderingStore.setState({ ordering });
  }, [enabled, ordering]);
  useEffect(() => {
    if (!enabled) return;
    return () => projectOrderingStore.setState({ ordering: null });
  }, [enabled]);
}

export function useProjectOrdering(): ProjectOrdering | null {
  return projectOrderingStore((state) => state.ordering);
}
