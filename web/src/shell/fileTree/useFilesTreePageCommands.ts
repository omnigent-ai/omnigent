// The Files tree's ⌘K commands for a page that can hide its tree: Reveal Active
// File and the tree root's commands stay while the tree is hidden, and running one
// asks the page to show the tree first (see `useFilesTreeShowRequests`).

import { useId, useMemo } from "react";
import { useSession } from "@/hooks/useSession";
import { relativizeToWorkspace, useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { usePaletteCommands } from "@/store/paletteCommands";
import { useRememberedBrowseLocation } from "../filesPane/browseLocations";
import {
  planActiveFileReveal,
  requestActiveFileReveal,
  revealActiveFilePaletteCommand,
} from "./revealActiveFile";
import {
  planActiveFolderAsTreeRoot,
  planWorkingFolderReturn,
  requestFilesTreeShown,
  treeRootPaletteCommands,
} from "./treeRoot";

export function useFilesTreePageCommands({
  conversationId,
  activePath,
  showHidden,
  enabled,
}: {
  /** The session, or undefined while none is open, which offers nothing. */
  conversationId: string | undefined;
  /** The file the page shows, workspace-relative or absolute. */
  activePath: string | null;
  showHidden: boolean;
  /** Whether the page is on screen; a page off screen offers nothing. */
  enabled: boolean;
}): void {
  const source = useId();
  const on = enabled && conversationId !== undefined;
  const environment = useWorkspaceEnvironment(conversationId, { enabled: on });
  const { session } = useSession(conversationId);
  const workspaceRoot = environment.data?.root ?? null;
  const browsed = useRememberedBrowseLocation(conversationId);
  // The same plans the tree's own header and menus follow, for the folder it browses.
  const reveal = planActiveFileReveal({
    activePath,
    location: relativizeToWorkspace(browsed, workspaceRoot),
    workspaceRoot,
    showHidden,
  });
  const canBrowseOutside = isOwnerLevel(session?.permissionLevel ?? null);
  const activeFolder = planActiveFolderAsTreeRoot({
    activePath,
    browsed,
    workspaceRoot,
    canBrowseOutside,
  });
  const workingFolder = planWorkingFolderReturn({ browsed, workspaceRoot });
  const signature = on
    ? JSON.stringify({ reveal: reveal.availability, activeFolder, workingFolder })
    : "";
  const commands = useMemo(() => {
    if (signature === "" || conversationId === undefined) return [];
    const facts = JSON.parse(signature) as {
      reveal: typeof reveal.availability;
      activeFolder: typeof activeFolder;
      workingFolder: typeof workingFolder;
    };
    return [
      revealActiveFilePaletteCommand(facts.reveal, () => {
        requestFilesTreeShown(conversationId);
        requestActiveFileReveal(conversationId, activePath);
      }),
      ...treeRootPaletteCommands(conversationId, facts),
    ];
  }, [signature, conversationId, activePath]);
  // Each page registers on its own, so one going off screen leaves the others' rows.
  usePaletteCommands(`files-tree-page:${source}`, "Files", commands);
}
