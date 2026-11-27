// Which repository the Diffs review shows: the one holding the session's directory, which
// Files shares, such as a worktree inside the workspace. The repository's name in the
// header opens the same folder browser Files does; choosing a folder moves both.

import { FolderOpenIcon, FolderSearchIcon } from "lucide-react";
import { useCallback, useId, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { useSession } from "@/hooks/useSession";
import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { useSessionWorktrees } from "@/hooks/useSessionWorktrees";
import { useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { parentIdFromLabels, sessionRoutingHostId } from "@/lib/sessionHost";
import {
  diffRootFor,
  repositoryFolderOf,
  workspaceInsideLargerRepository,
} from "@/lib/repositoryFolder";
import { workspaceKeyOf } from "@/lib/workingCopyComparisons";
import {
  AVAILABLE,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import { browseNavigation } from "./BrowseLocationBar";
import { rememberBrowseLocation, useRememberedBrowseLocation } from "./filesPane/browseLocations";
import { useSessionHostId } from "./fileCommands/target";
import { useWorkspacePaths } from "./FileViewerContext";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";

export const CHOOSE_DIFFS_FOLDER_LABEL = "Choose Folder for Diffs…";
/** Why a workspace inside a larger repository can't choose another folder for Diffs. */
export const LARGER_REPOSITORY_REASON =
  "Diffs shows only this workspace, because it's a folder inside a larger repository.";

/** A folder's last name, as the header shows it. */
export function folderName(path: string | null): string {
  if (!path) return "Workspace";
  return path.replace(/\/+$/, "").split("/").at(-1) || path;
}

/** The repository a session's Diffs review shows, and how to choose another. */
export interface ReviewFolder {
  /** The workspace root, once known. */
  workspace: string | null;
  /** The repository reviewed, absolute: the workspace, or a worktree in the session's folders. */
  root: string | null;
  /**
   * `root` relative to the workspace, as the host names it: empty for the workspace, null
   * when it lies outside the workspace, where Diffs can't review it.
   */
  repository: string | null;
  /** Which host and workspace `repository` lies in, so same-named folders elsewhere stay apart. */
  workspaceKey: string;
  /** The session's directory, which Files shares: absolute, or null for the workspace root. */
  directory: string | null;
  /** Whether this viewer may choose another folder. */
  canChoose: boolean;
  /** Why the folder can't be chosen here when the viewer otherwise could; null when it can. */
  cannotChooseReason: string | null;
  /**
   * `root`, `rootFor` and `canChoose` rest on answers: false while a lookup is in flight or
   * failed, since neither says which folders exist.
   */
  resolved: boolean;
  /** The repository Diffs would review for `directory`: absolute, or null for the workspace. */
  rootFor: (directory: string | null) => string | null;
  /** Opens the folder browser, where the viewer may choose. */
  choose: () => void;
  /** Returns Diffs, and Files with it, to the workspace. */
  showWorkspace: () => void;
  /** The browser, mounted once wherever the review renders. */
  dialog: React.ReactNode;
}

// The last worktree list each session's workspace had, by session, host and workspace.
const lastWorktreeLists = new Map<string, HostWorktree[]>();

/** Forgets every kept worktree list, between tests. */
export function resetReviewFolderListsForTests(): void {
  lastWorktreeLists.clear();
}

export function useReviewFolder(
  conversationId: string,
  active: boolean,
  /** A folder the review may move to, such as a visit's worktree, whose worktree to look up. */
  lookUp: string | null = null,
): ReviewFolder {
  const { root: workspace } = useWorkspacePaths();
  const hostId = useSessionHostId();
  const directory = useRememberedBrowseLocation(conversationId);
  // The worktree list tells which folders Diffs may review, so it's needed while Diffs shows,
  // once Files or Diffs moved off the workspace, or for a folder the review may move to.
  const needsWorktrees = active || directory !== null || lookUp !== null;
  const worktrees = useSessionWorktrees(conversationId, hostId, needsWorktrees ? workspace : null);
  const answer = worktrees.data;
  // Only the host's owner may list worktrees; anyone else reviews the workspace itself.
  const denied = answer?.status === "unknown" && answer.denied === true;
  // A failed lookup keeps the list this workspace last had, in every review of the session, so
  // a review doesn't jump to the workspace and record that over its visit.
  const lookupKey = `${conversationId}\u0000${hostId ?? ""}\u0000${workspace ?? ""}`;
  if (answer?.status === "ok") lastWorktreeLists.set(lookupKey, answer.worktrees);
  // "Not a Git repository" and a refusal rule the old list out, so a later failure can't revive it.
  else if (answer?.status === "not_git" || denied) lastWorktreeLists.delete(lookupKey);
  const kept =
    answer?.status === "unknown" && !denied ? (lastWorktreeLists.get(lookupKey) ?? null) : null;
  const listed = answer?.status === "ok" ? answer.worktrees : kept;
  const { session, isLoading: sessionLoading } = useSession(conversationId);
  // A top-level session with no host has no worktrees to list; a child waits for its parent's host.
  const hostless =
    hostId === null &&
    session != null &&
    sessionRoutingHostId(session.hostId, session.labels) === null &&
    (session.parentSessionId ?? parentIdFromLabels(session.labels)) === null;
  // Only a list, "not a Git repository", a refusal or having no host says which folders Diffs
  // can review.
  const worktreesKnown = listed !== null || answer?.status === "not_git" || denied || hostless;
  const insideLargerRepository =
    workspace !== null && listed !== null && workspaceInsideLargerRepository(workspace, listed);
  const root = workspace ? diffRootFor(directory, workspace, listed ?? []) : null;
  const repository = root && workspace ? repositoryFolderOf(root, workspace) : "";
  const rootFor = (candidate: string | null): string | null => {
    if (!workspace) return null;
    const reviewed = diffRootFor(candidate, workspace, listed ?? []);
    return repositoryFolderOf(reviewed, workspace) ? reviewed : null;
  };
  const environment = useWorkspaceEnvironment(conversationId);
  const resolved =
    workspace !== null &&
    !sessionLoading &&
    !environment.isLoading &&
    (!needsWorktrees || worktreesKnown);
  const canRoam =
    workspace !== null &&
    browseNavigation({
      current: root ?? workspace,
      workspace,
      hostId,
      canBrowseOutside: isOwnerLevel(session?.permissionLevel ?? null),
      reach: environment.data?.reachable ?? null,
    }).canRoam;
  const cannotChooseReason = canRoam && insideLargerRepository ? LARGER_REPOSITORY_REASON : null;
  // A workspace inside a larger repository can't choose, so choosing waits until that's known.
  const canChoose = canRoam && worktreesKnown && !insideLargerRepository;
  const [open, setOpen] = useState(false);
  const paletteSource = useId();
  const showWorkspace = useCallback(
    () => rememberBrowseLocation(conversationId, null),
    [conversationId],
  );
  const dialog =
    canChoose && workspace ? (
      <WorkspacePickerDialog
        open={open}
        onOpenChange={setOpen}
        hostId={hostId}
        initialPath={root ?? workspace}
        workspacePath={workspace}
        title="Choose Folder for Diffs"
        description="Diffs and Files show the folder you choose. A worktree inside the workspace shows its own changes."
        onConfirm={(path) =>
          rememberBrowseLocation(conversationId, path === workspace ? null : path)
        }
      />
    ) : null;
  const awayFromWorkspace = repository !== "";
  const commands = useMemo(
    (): PaletteCommand[] =>
      active && (canChoose || cannotChooseReason)
        ? [
            {
              id: `choose-diffs-folder:${conversationId}`,
              label: CHOOSE_DIFFS_FOLDER_LABEL,
              icon: FolderSearchIcon,
              keywords: ["worktree", "folder", "directory", "repository", "diffs", "changes"],
              availability: cannotChooseReason ? unavailableBecause(cannotChooseReason) : AVAILABLE,
              movesFocus: true,
              run: () => setOpen(true),
            },
            ...(awayFromWorkspace
              ? [
                  {
                    id: `diffs-show-workspace:${conversationId}`,
                    label: `Show ${folderName(workspace)} in Diffs`,
                    icon: FolderOpenIcon,
                    keywords: ["worktree", "workspace", "folder", "diffs", "changes"],
                    availability: AVAILABLE,
                    run: showWorkspace,
                  },
                ]
              : []),
          ]
        : [],
    [
      active,
      canChoose,
      cannotChooseReason,
      conversationId,
      awayFromWorkspace,
      workspace,
      showWorkspace,
    ],
  );
  // Each review registers apart: a hidden Diffs tab of the same session can't withdraw these.
  usePaletteCommands(
    `changes-review-folder:${conversationId}:${paletteSource}`,
    "Changes",
    commands,
  );
  return {
    workspace,
    root,
    repository,
    workspaceKey: workspace ? workspaceKeyOf(hostId, workspace) : "",
    directory,
    canChoose,
    cannotChooseReason,
    resolved,
    rootFor,
    choose: () => setOpen(true),
    showWorkspace,
    dialog,
  };
}

/** The repository's name in the Diffs header: the folder browser, where the viewer may choose. */
export function ReviewFolderName({ folder }: { folder: ReviewFolder }) {
  const name = folderName(folder.root);
  const path = folder.root ?? name;
  if (!folder.canChoose) {
    const reason = folder.cannotChooseReason;
    return (
      <span
        className="min-w-0 shrink-[3] truncate font-semibold"
        title={reason ? `${path}\n${reason}` : path}
        aria-description={reason ?? undefined}
        data-testid={reason ? "changes-review-folder-fixed" : undefined}
      >
        {name}
      </span>
    );
  }
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-6 min-w-0 shrink-[3] justify-start px-1 font-semibold"
      title={path}
      aria-label={`Diffs folder: ${path}. Choose another folder.`}
      data-testid="changes-review-folder"
      onClick={folder.choose}
    >
      <span className="truncate">{name}</span>
    </Button>
  );
}
