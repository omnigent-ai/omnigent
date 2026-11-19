import { settingsEntry } from "./types";

export const ALWAYS_USE_WORKTREE_ENTRY = settingsEntry({
  id: "always-use-worktree",
  section: "git",
  label: "Always use a random worktree",
  description:
    "Use a fresh, randomly named worktree for new sessions in Git workspaces. Project settings override this default.",
  keywords: ["worktree", "git", "branch", "new session", "isolate"],
});

export const DELETE_WORKTREES_ON_ARCHIVE_ENTRY = settingsEntry({
  id: "delete-worktrees-on-archive",
  available: (availability) => availability.archiveWorktreeCleanup,
  section: "git",
  label: "Delete worktrees for archived sessions",
  description:
    "Delete the session's worktree and any uncommitted changes when archived. The branch is kept.",
  keywords: ["worktree", "archive", "delete", "cleanup", "git"],
});

export const DEFAULT_BASE_BRANCH_ENTRY = settingsEntry({
  id: "default-base-branch",
  section: "git",
  label: "Default base branch",
  description: "Prefill the base branch for new worktree branches. Leave blank for no prefill.",
  keywords: ["branch", "main", "base", "worktree", "git"],
});

export const GIT_SETTINGS = [
  ALWAYS_USE_WORKTREE_ENTRY,
  DEFAULT_BASE_BRANCH_ENTRY,
  DELETE_WORKTREES_ON_ARCHIVE_ENTRY,
] as const;
