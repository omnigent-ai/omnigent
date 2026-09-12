import { Loader2Icon, RefreshCwIcon } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ComposerWorkspaceTrigger } from "@/components/composer/ComposerControls";
import type { ComposerBranchState } from "@/hooks/useComposerGitStatus";

/** Trailing path segment, e.g. ``feature-login``. */
function pathTail(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

/** Trigger label for each branch state — no state is dressed up as another. */
function branchLabel(state: ComposerBranchState, branch: string | null): string {
  switch (state) {
    case "branch":
      return branch ?? "No branch";
    case "detached":
      return "Detached HEAD";
    case "not-git":
      return "Not a Git repository";
    case "loading":
      return "Checking branch…";
    case "unknown":
      return "Branch unavailable";
  }
}

/** Popover body explaining the branch state honestly. */
function branchDetail(state: ComposerBranchState, branch: string | null): string {
  switch (state) {
    case "branch":
      return branch ?? "";
    case "detached":
      return "Detached HEAD — no branch is checked out in this workspace.";
    case "not-git":
      return "This workspace is not a Git repository.";
    case "loading":
      return "Reading the branch from the workspace…";
    case "unknown":
      // Covers offline hosts, ambiguous git failures, empty and unmatched
      // results alike — stay reason-neutral, never blame the host.
      return "The workspace branch could not be determined.";
  }
}

/**
 * Read-only workspace + branch status for a running session's composer bar.
 *
 * Renders two distinct, correctly-labelled triggers: the working DIRECTORY
 * (headed "Worktree" when the workspace is a linked worktree, else
 * "Workspace") and the git BRANCH (headed "Git branch"), replacing the earlier
 * bar that paired a branch value with a "Session worktree" heading and showed
 * only the static creation-time branch. The branch reflects the live checkout
 * state — a real branch, detached HEAD, non-git, or unavailable — and never
 * silently substitutes the creation-time branch, which is shown separately as
 * history. Session-scoped: the landing surface keeps its own worktree picker.
 */
export function ComposerWorkspaceStatus({
  workspacePath,
  worktreePath,
  isWorktree,
  branch,
  branchState,
  creationBranch,
  onRefreshBranch,
  refreshing = false,
}: {
  workspacePath: string | null;
  worktreePath: string | null;
  isWorktree: boolean | null;
  branch: string | null;
  branchState: ComposerBranchState;
  creationBranch: string | null;
  onRefreshBranch?: () => void;
  refreshing?: boolean;
}) {
  const dirHeading = isWorktree ? "Worktree" : "Workspace";
  // Note the creation-time branch only when it adds information the live line
  // doesn't already show (a different name, or no live branch resolved).
  const showCreation =
    creationBranch !== null && (branchState !== "branch" || creationBranch !== branch);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <ComposerWorkspaceTrigger
            kind="directory"
            label={workspacePath ? pathTail(workspacePath) : "No workspace"}
            title={workspacePath ?? "No workspace bound"}
            data-testid="composer-workspace-dir"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          side="top"
          className="max-w-[min(90vw,28rem)] whitespace-normal"
        >
          <DropdownMenuLabel>{dirHeading}</DropdownMenuLabel>
          <p className="break-all px-2 py-1 text-xs text-muted-foreground">
            {workspacePath ?? "This session has no workspace binding."}
          </p>
          {isWorktree && worktreePath && worktreePath !== workspacePath ? (
            <p className="break-all px-2 py-1 text-xs text-muted-foreground">
              Linked worktree at {worktreePath}.
            </p>
          ) : null}
          <p className="px-2 py-1 text-xs text-muted-foreground">
            {isWorktree
              ? "A linked git worktree the session runs in."
              : "The working directory the session runs in."}
          </p>
        </DropdownMenuContent>
      </DropdownMenu>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <ComposerWorkspaceTrigger
            kind="worktree"
            label={branchLabel(branchState, branch)}
            data-testid="composer-git-branch"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          side="top"
          className="max-w-[min(90vw,28rem)] whitespace-normal"
        >
          <div className="flex items-center justify-between gap-2 pr-1">
            <DropdownMenuLabel>Git branch</DropdownMenuLabel>
            {onRefreshBranch ? (
              <button
                type="button"
                onClick={(event) => {
                  // Keep the popover open so the refreshed value stays visible.
                  event.preventDefault();
                  onRefreshBranch();
                }}
                disabled={refreshing}
                data-testid="composer-git-branch-refresh"
                aria-label="Refresh branch"
                title="Refresh branch"
                className="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-default disabled:opacity-50"
              >
                {refreshing ? (
                  <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
                ) : (
                  <RefreshCwIcon className="size-3.5" aria-hidden />
                )}
              </button>
            ) : null}
          </div>
          <p className="break-all px-2 py-1 text-xs text-muted-foreground">
            {branchDetail(branchState, branch)}
          </p>
          {showCreation ? (
            <p className="break-all px-2 py-1 text-xs text-muted-foreground">
              Created on branch {creationBranch}.
            </p>
          ) : null}
        </DropdownMenuContent>
      </DropdownMenu>
    </>
  );
}
