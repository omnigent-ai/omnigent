// Per-device choice of whether a project opens its sessions board (the default)
// or keeps the folder rows and New session composer from before the board.

import { isFeatureEnabled } from "./capabilities";
import { useServerInfo } from "./CapabilitiesContext";
import { createStoredPreference } from "./storedChoicePreference";

export const PROJECT_KANBAN_BOARD_STORAGE_KEY = "omnigent:project-kanban-board";

export const DEFAULT_PROJECT_KANBAN_BOARD = true;

const boardPreference = createStoredPreference<boolean>(
  PROJECT_KANBAN_BOARD_STORAGE_KEY,
  (stored) => (stored === "true" || stored === "false" ? stored === "true" : undefined),
  DEFAULT_PROJECT_KANBAN_BOARD,
);

export const readProjectKanbanBoard = boardPreference.read;
export const writeProjectKanbanBoard = boardPreference.write;

/** Live switch value, including changes made in other windows. */
export function useProjectKanbanBoardPreference(): boolean {
  return boardPreference.use();
}

/** Whether projects open their board: the server offers it and this device keeps it on. */
export function useProjectKanbanEnabled(): boolean {
  const info = useServerInfo();
  const preferred = boardPreference.use();
  return preferred && isFeatureEnabled(info, "project_kanban");
}
