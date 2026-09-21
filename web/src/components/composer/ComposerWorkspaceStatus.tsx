import { FolderIcon, GitForkIcon } from "lucide-react";

import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "./ChatComposer";
import type { ComposerBranchState } from "@/hooks/useComposerGitStatus";
import { cn } from "@/lib/utils";

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

/** Read-only workspace identity for an existing session's composer bar. */
export function ComposerWorkspaceStatus({
  workspacePath,
  worktreePath,
  isWorktree,
  branch,
  branchState,
  creationBranch,
  showWorktree,
}: {
  workspacePath: string | null;
  worktreePath: string | null;
  isWorktree: boolean | null;
  branch: string | null;
  branchState: ComposerBranchState;
  creationBranch: string | null;
  showWorktree: boolean;
}) {
  const branchText = branchLabel(branchState, branch);
  const branchTitle =
    creationBranch && (branchState !== "branch" || creationBranch !== branch)
      ? `${branchText}. Created on branch ${creationBranch}.`
      : branchText;

  return (
    <>
      <WorkspaceStatusItem
        icon={FolderIcon}
        label={workspacePath ? pathTail(workspacePath) : "No workspace"}
        title={workspacePath ?? "No workspace bound"}
        testId="composer-workspace-dir"
      />
      {showWorktree ? (
        <WorkspaceStatusItem
          icon={GitForkIcon}
          label={branchText}
          title={
            isWorktree && worktreePath ? `${branchTitle} Worktree: ${worktreePath}` : branchTitle
          }
          testId="composer-git-branch"
        />
      ) : null}
    </>
  );
}

function WorkspaceStatusItem({
  icon: Icon,
  label,
  title,
  testId,
}: {
  icon: typeof FolderIcon;
  label: string;
  title: string;
  testId: string;
}) {
  return (
    <span
      className="relative inline-flex h-6 min-w-0 max-w-[calc(50%-0.25rem)] items-center gap-1 px-0.5 text-xs leading-4 font-normal text-muted-foreground md:px-1"
      title={title}
      data-testid={testId}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden />
      <span
        data-workspace-collapse-label=""
        className={cn("min-w-0 truncate text-left", COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS)}
      >
        {label}
      </span>
    </span>
  );
}
