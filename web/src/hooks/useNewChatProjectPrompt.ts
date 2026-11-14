import { useCallback, useEffect } from "react";

import { useProjects } from "@/hooks/useConversations";
import { readNewChatProjectPromptEnabled } from "@/lib/newChatProjectPromptPreferences";
import { useNavigate } from "@/lib/routing";

/**
 * Returns a check for whether starting a new session should first ask for a
 * project: the project list may hold one and the prompt isn't turned off in
 * Settings. Until the list resolves it counts as "may": the picker lists
 * projects as they arrive and steps aside if none do (see
 * `useLeaveProjectPickerWithoutProjects`).
 * The preference is read at call time so a toggle applies at once.
 */
export function useShouldPromptForProject(): () => boolean {
  const { data: projects } = useProjects();
  const mayHaveProjects = projects === undefined || projects.length > 0;
  return useCallback(() => mayHaveProjects && readNewChatProjectPromptEnabled(), [mayHaveProjects]);
}

/** The new-session composer route for `projectName`, or the plain one for "". */
export function newSessionPath(projectName: string): string {
  return projectName ? `/?project=${encodeURIComponent(projectName)}` : "/";
}

/**
 * While a project picker is `active`, closes it for the plain composer once the
 * project list fails or comes back empty: with nothing to choose from, the
 * session starts as if the prompt had never been offered.
 */
export function useLeaveProjectPickerWithoutProjects(
  active: boolean,
  onOpenChange: (open: boolean) => void,
): void {
  const { data: projects, isSuccess, isError } = useProjects();
  const navigate = useNavigate();
  const nothingToOffer = isError || (isSuccess && (projects?.length ?? 0) === 0);
  useEffect(() => {
    if (!active || !nothingToOffer) return;
    onOpenChange(false);
    navigate("/");
  }, [active, nothingToOffer, onOpenChange, navigate]);
}
