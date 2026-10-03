import { FolderIcon, GitForkIcon } from "lucide-react";

import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "./ChatComposer";
import type { ComposerBranchState } from "@/hooks/useComposerGitStatus";
import { cn } from "@/lib/utils";

/** Trailing path segment, e.g. ``feature-login``. */
function pathTail(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

/**
 * Repo name[#branch] of one ``<url>[#<branch>]`` sandbox workspace, matching the landing chip.
 */
function sandboxRepoTail(workspace: string): string {
  const hash = workspace.indexOf("#");
  const url = (hash === -1 ? workspace : workspace.slice(0, hash)).replace(/\/+$/, "");
  const last = url.split(/[/:]/).pop() ?? url;
  const name = last.endsWith(".git") ? last.slice(0, -4) : last;
  return hash === -1 ? name : `${name}#${workspace.slice(hash + 1)}`;
}

// Folder item texts: the bound directory; else the repos a managed launch clones, named while
// no workspace is bound (including after a launch that failed before binding one); else none.
function workspaceItem(
  workspacePath: string | null,
  sandboxRepos: string[],
): { label: string; title: string; ariaLabel: string } {
  if (workspacePath) {
    const title = `Working directory: ${workspacePath}`;
    return { label: pathTail(workspacePath), title, ariaLabel: title };
  }
  if (sandboxRepos.length === 1) {
    const title = `Sandbox repository: ${sandboxRepos[0]}`;
    return { label: sandboxRepoTail(sandboxRepos[0]), title, ariaLabel: title };
  }
  if (sandboxRepos.length > 1) {
    const title = `Sandbox repositories: ${sandboxRepos.join(", ")}`;
    return { label: `${sandboxRepos.length} repositories`, title, ariaLabel: title };
  }
  return {
    label: "No workspace",
    title: "No working directory bound",
    ariaLabel: "Working directory: Not selected",
  };
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
  sandboxRepos,
  worktreePath,
  isWorktree,
  branch,
  branchState,
  creationBranch,
  showWorktree,
}: {
  workspacePath: string | null;
  /** ``<url>[#<branch>]`` repositories the managed launch was asked to clone. */
  sandboxRepos: string[];
  worktreePath: string | null;
  isWorktree: boolean | null;
  branch: string | null;
  branchState: ComposerBranchState;
  creationBranch: string | null;
  showWorktree: boolean;
}) {
  const directory = workspaceItem(workspacePath, sandboxRepos);
  const branchText = branchLabel(branchState, branch);
  const branchTitle =
    creationBranch && (branchState !== "branch" || creationBranch !== branch)
      ? `${branchText}. Created on branch ${creationBranch}.`
      : branchText;

  return (
    <>
      <WorkspaceStatusItem
        icon={FolderIcon}
        label={directory.label}
        title={directory.title}
        ariaLabel={directory.ariaLabel}
        testId="composer-workspace-dir"
      />
      {showWorktree ? (
        <WorkspaceStatusItem
          icon={GitForkIcon}
          label={branchText}
          title={
            isWorktree && worktreePath ? `Worktree: ${worktreePath}. ${branchTitle}` : branchTitle
          }
          ariaLabel={`Worktree: ${branchText}`}
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
  ariaLabel,
  testId,
}: {
  icon: typeof FolderIcon;
  label: string;
  title: string;
  ariaLabel: string;
  testId: string;
}) {
  return (
    <span
      className="relative inline-flex h-6 min-w-0 max-w-[calc(50%-0.25rem)] items-center gap-1 px-1 text-xs leading-4 font-normal text-muted-foreground"
      title={title}
      aria-label={ariaLabel}
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
