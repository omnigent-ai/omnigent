// ⌘K's Show all pull requests for the tabs sidebar. The app shell mounts it only in tabs mode,
// so it works while the sidebar is closed, and it runs the same open as the composer's PR chip.

import { GitPullRequestIcon } from "lucide-react";
import { useMemo, useRef, useSyncExternalStore } from "react";

import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";

import { isUnknownTab } from "../workspaceTabs/workspaceTab";
import { workspaceTabsSession } from "../workspaceTabs/workspaceTabsSessions";
import { showAllPullRequestsCommand } from "./model";

export interface ShowAllPullRequestsCommandProps {
  /** The routed session in the tabs sidebar. */
  sessionId: string;
  /** The session's workspace can show pull requests. */
  pullRequestsAvailable: boolean;
  /** Reveal the sidebar and open or select the session's pull requests tab. */
  showAll: () => void;
}

export function useShowAllPullRequestsCommand({
  sessionId,
  pullRequestsAvailable,
  showAll,
}: ShowAllPullRequestsCommandProps): void {
  const showAllRef = useRef(showAll);
  showAllRef.current = showAll;
  const store = useMemo(() => workspaceTabsSession(sessionId).store, [sessionId]);
  const hasPullRequestTab = useSyncExternalStore(store.subscribe, () =>
    store
      .tabs()
      .some(
        (tab) => !isUnknownTab(tab) && (tab.place.kind === "pulls" || tab.place.kind === "pull"),
      ),
  );
  const commands = useMemo<PaletteCommand[]>(() => {
    const command = showAllPullRequestsCommand({ pullRequestsAvailable, hasPullRequestTab });
    return [
      {
        // The session is part of the id, so a row built for another session never runs.
        id: `workspace:${sessionId}:pull-requests:all`,
        label: command.label,
        icon: GitPullRequestIcon,
        keywords: [...command.keywords],
        availability: command.availability,
        // It selects or opens a tab, so it runs once the palette has closed.
        movesFocus: true,
        run: () => showAllRef.current(),
      },
    ];
  }, [sessionId, pullRequestsAvailable, hasPullRequestTab]);
  usePaletteCommands(`workspace-pull-requests:${sessionId}`, "Workspace", commands);
}

/** Registers the row while mounted; the app shell mounts it only for a tabs sidebar session. */
export function ShowAllPullRequestsCommand(props: ShowAllPullRequestsCommandProps): null {
  useShowAllPullRequestsCommand(props);
  return null;
}
