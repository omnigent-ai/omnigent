// The Files tree's root: the folder it browses, chosen from a breadcrumb, a tree
// row or ⌘K, and the way back to the working folder. Every entry point plans with
// these functions and moves the root through `showTreeRoot`, so they agree.

import { FolderDotIcon, FolderInputIcon } from "lucide-react";
import { useEffect, useRef } from "react";
import {
  type CommandAvailability,
  type PaletteCommand,
  AVAILABLE,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";
import { relativizeToWorkspace } from "@/hooks/useWorkspaceChangedFiles";
import { absoluteWorkspacePath } from "@/lib/composerContext";
import { parentFolder } from "../breadcrumbPaths";
import { rememberBrowseLocation } from "../filesPane/browseLocations";

/** The ⌘K name for rooting the tree at the viewed file's folder. */
export const ACTIVE_FOLDER_AS_ROOT_LABEL = "Show Active File’s Folder as Tree Root";
/** The way back from a folder to the workspace, in the header, its menus and ⌘K. */
export const BACK_TO_WORKING_FOLDER_LABEL = "Back to working folder";
export const NO_ACTIVE_FOLDER_REASON = "Open a file to show its folder as the tree root.";
export const ALREADY_ROOT_REASON = "The Files tree already shows this folder.";
export const OUTSIDE_REACH_REASON = "Only the session’s owner can browse outside its workspace.";
export const AT_WORKING_FOLDER_REASON = "The Files tree already shows the working folder.";

export interface TreeRootPlan {
  availability: CommandAvailability;
  /** What to remember as the browsed folder: absolute, or null for the workspace root. */
  root: string | null;
}

const trimSlash = (path: string) => (path.length > 1 ? path.replace(/\/+$/, "") : path);

/**
 * `browsed` in the panel's wire form: "" for the workspace root, relative inside it,
 * absolute outside. Comparing in that form makes "/repo", "/repo/" and null agree.
 */
function locationOf(browsed: string | null, workspaceRoot: string): string {
  return relativizeToWorkspace(browsed === null ? null : trimSlash(browsed), workspaceRoot);
}

/**
 * Whether the tree can be rooted at `folder` (workspace-relative or absolute) while it
 * browses `browsed` (absolute, or null for the workspace root), and what to remember.
 */
export function planFolderAsTreeRoot({
  folder,
  browsed,
  workspaceRoot,
  canBrowseOutside,
}: {
  folder: string;
  browsed: string | null;
  workspaceRoot: string | null;
  canBrowseOutside: boolean;
}): TreeRootPlan {
  if (!workspaceRoot) return { availability: HIDDEN, root: null };
  const absolute =
    folder === "" ? workspaceRoot : absoluteWorkspacePath(trimSlash(folder), workspaceRoot);
  if (absolute === null) return { availability: HIDDEN, root: null };
  const location = locationOf(absolute, workspaceRoot);
  const root = location === "" ? null : trimSlash(absolute);
  if (location === locationOf(browsed, workspaceRoot)) {
    return { availability: unavailableBecause(ALREADY_ROOT_REASON), root };
  }
  // Outside the workspace the location stays absolute, which only owners may browse.
  if (!canBrowseOutside && location.startsWith("/")) {
    return { availability: unavailableBecause(OUTSIDE_REACH_REASON), root };
  }
  return { availability: AVAILABLE, root };
}

/** Rooting the tree at the viewed file's folder. */
export function planActiveFolderAsTreeRoot({
  activePath,
  ...context
}: {
  activePath: string | null | undefined;
  browsed: string | null;
  workspaceRoot: string | null;
  canBrowseOutside: boolean;
}): TreeRootPlan {
  if (!activePath) return { availability: unavailableBecause(NO_ACTIVE_FOLDER_REASON), root: null };
  return planFolderAsTreeRoot({ folder: parentFolder(activePath), ...context });
}

/** Returning the tree to the workspace root, offered only while it browses elsewhere. */
export function planWorkingFolderReturn({
  browsed,
  workspaceRoot,
}: {
  browsed: string | null;
  workspaceRoot: string | null;
}): TreeRootPlan {
  const away = workspaceRoot !== null && locationOf(browsed, workspaceRoot) !== "";
  return {
    availability: away ? AVAILABLE : unavailableBecause(AT_WORKING_FOLDER_REASON),
    root: null,
  };
}

/** The two root commands' ⌘K rows, the same wherever they're registered. */
export function treeRootPaletteCommands(
  conversationId: string,
  { activeFolder, workingFolder }: { activeFolder: TreeRootPlan; workingFolder: TreeRootPlan },
): PaletteCommand[] {
  return [
    {
      id: "files.active-folder-as-root",
      label: ACTIVE_FOLDER_AS_ROOT_LABEL,
      icon: FolderInputIcon,
      keywords: ["files", "tree", "root", "folder", "scope", "browse"],
      // A hidden tree may open as a popover, which the palette's closing focus would dismiss.
      movesFocus: true,
      availability: activeFolder.availability,
      run: () => showTreeRoot(conversationId, activeFolder.root),
    },
    {
      id: "files.working-folder",
      label: BACK_TO_WORKING_FOLDER_LABEL,
      icon: FolderDotIcon,
      keywords: ["files", "tree", "root", "workspace", "reset", "home"],
      movesFocus: true,
      availability: workingFolder.availability,
      run: () => showTreeRoot(conversationId, null),
    },
  ];
}

/** Pages showing a session's Files tree, asked to put it on screen. */
const showListeners = new Set<(conversationId: string) => void>();

/** Asks the page showing the session's Files tree to put it on screen, as after a new root. */
export function requestFilesTreeShown(conversationId: string): void {
  showListeners.forEach((listener) => listener(conversationId));
}

/** Roots the session's Files tree at `root` (null: the workspace) and shows the tree. */
export function showTreeRoot(conversationId: string, root: string | null): void {
  rememberBrowseLocation(conversationId, root);
  requestFilesTreeShown(conversationId);
}

/**
 * Calls `show` when something asks for this session's tree, so a hidden one comes on
 * screen. Each page that hosts the tree listens while on screen: the Files tab in the
 * tab design, the rail or the phone drawer in the classic one.
 */
export function useFilesTreeShowRequests(conversationId: string | undefined, show: () => void) {
  const latest = useRef(show);
  latest.current = show;
  useEffect(() => {
    if (!conversationId) return;
    const listener = (requested: string) => {
      if (requested === conversationId) latest.current();
    };
    showListeners.add(listener);
    return () => {
      showListeners.delete(listener);
    };
  }, [conversationId]);
}
