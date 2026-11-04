import { CheckIcon, FolderIcon, GitForkIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS } from "./ChatComposer";
import type { ComposerBranchState } from "@/hooks/useComposerGitStatus";
import { copyText } from "@/lib/clipboard";
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

/** Workspace identity for an existing session's composer bar; the directory copies its path. */
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
      {workspacePath ? (
        <WorkspaceDirectoryCopyButton path={workspacePath} />
      ) : (
        <WorkspaceStatusItem
          icon={FolderIcon}
          label="No workspace"
          title="No working directory bound"
          ariaLabel="Working directory: Not selected"
          testId="composer-workspace-dir"
        />
      )}
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

const WORKSPACE_STATUS_ITEM_CLASS =
  "relative inline-flex h-6 min-w-0 max-w-[calc(50%-0.25rem)] items-center gap-1 px-1 text-xs leading-4 font-normal text-muted-foreground";

/**
 * The bound working directory, shown by its tail and copied in full on click.
 * Feedback stays in place (check icon, then the title) like CopyPathButton.
 */
function WorkspaceDirectoryCopyButton({ path }: { path: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const timerRef = useRef<number>(0);

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  const handleClick = async () => {
    window.clearTimeout(timerRef.current);
    try {
      await copyText(path);
      setCopyState("copied");
      timerRef.current = window.setTimeout(() => setCopyState("idle"), 2000);
    } catch {
      setCopyState("failed");
      timerRef.current = window.setTimeout(() => setCopyState("idle"), 3000);
    }
  };

  const title =
    copyState === "copied"
      ? `Copied: ${path}`
      : copyState === "failed"
        ? "Copy failed"
        : `Working directory: ${path}. Click to copy.`;
  const Icon = copyState === "copied" ? CheckIcon : FolderIcon;

  return (
    <button
      type="button"
      className={cn(
        WORKSPACE_STATUS_ITEM_CLASS,
        "cursor-pointer rounded hover:bg-muted hover:text-foreground",
        copyState === "copied" && "text-green-600 dark:text-green-400",
        copyState === "failed" && "text-destructive",
      )}
      title={title}
      aria-label={`Copy working directory: ${path}`}
      data-testid="composer-workspace-dir"
      onClick={() => void handleClick()}
    >
      <Icon className="size-3.5 shrink-0" aria-hidden />
      <span
        data-workspace-collapse-label=""
        className={cn("min-w-0 truncate text-left", COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS)}
      >
        {pathTail(path)}
      </span>
    </button>
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
      className={WORKSPACE_STATUS_ITEM_CLASS}
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
