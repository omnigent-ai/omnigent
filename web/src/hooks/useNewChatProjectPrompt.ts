import { useCallback } from "react";

import { useProjects } from "@/hooks/useConversations";
import { readNewChatProjectPromptEnabled } from "@/lib/newChatProjectPromptPreferences";

/**
 * Returns a check for whether starting a new session should first ask for a
 * project: the project list may hold one and the prompt isn't turned off in
 * Settings. Until the list resolves it counts as "may": the picker lists
 * projects as they arrive and steps aside if none do (see `CommandPalette`).
 * The preference is read at call time so a toggle applies at once.
 */
export function useShouldPromptForProject(): () => boolean {
  const { data: projects } = useProjects();
  const mayHaveProjects = projects === undefined || projects.length > 0;
  return useCallback(() => mayHaveProjects && readNewChatProjectPromptEnabled(), [mayHaveProjects]);
}
