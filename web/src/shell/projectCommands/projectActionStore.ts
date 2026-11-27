// Requests to carry out a project's folder action away from its folder, such as from ⌘K.
// The app-wide ProjectActionHost opens the action's dialog or navigates.

import { create } from "zustand";

import type { ProjectSummary } from "@/hooks/useConversations";

export type ProjectCommandId = "new-session" | "rename" | "settings" | "delete";

export interface ProjectActionRequest {
  project: ProjectSummary;
  command: ProjectCommandId;
  /** New per request, so asking twice for the same action opens it again. */
  key: number;
}

/** A project renamed from the host; each rename gets a new key so repeats are seen. */
export interface ProjectRename {
  oldName: string;
  newName: string;
  key: number;
}

export const useProjectActionStore = create<{
  request: ProjectActionRequest | null;
  lastRename: ProjectRename | null;
}>(() => ({ request: null, lastRename: null }));

let nextKey = 0;

/** Carry out `command` on `project`. */
export function requestProjectAction(project: ProjectSummary, command: ProjectCommandId): void {
  nextKey += 1;
  useProjectActionStore.setState({ request: { project, command, key: nextKey } });
}

/** Tell the sidebar a project was renamed, so its folder stays expanded under the new name. */
export function reportProjectRenamed(oldName: string, newName: string): void {
  nextKey += 1;
  useProjectActionStore.setState({ lastRename: { oldName, newName, key: nextKey } });
}

/** Mark a reported rename handled, so a remounted sidebar doesn't replay it. A newer one stays. */
export function consumeProjectRename(key: number): void {
  useProjectActionStore.setState((state) =>
    state.lastRename?.key === key ? { lastRename: null } : state,
  );
}
