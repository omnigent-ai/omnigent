import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import { archiveWorktreeTargets } from "./ArchivePrompt";

function session(id: string, fields: Partial<Conversation>): Conversation {
  return {
    id,
    object: "conversation",
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...fields,
  };
}

describe("archiveWorktreeTargets", () => {
  it("lists each worktree once in selection order and skips sessions without one", () => {
    const targets = archiveWorktreeTargets([
      session("a", { git_branch: "fix/b", workspace: "/repo/.worktrees/b" }),
      session("b", { workspace: "/repo" }),
      session("c", { git_branch: "feat/a", workspace: "/repo/.worktrees/a" }),
      session("d", { git_branch: "fix/b", workspace: "/repo/.worktrees/b" }),
    ]);

    expect(targets).toEqual([
      { path: "/repo/.worktrees/b", branch: "fix/b" },
      { path: "/repo/.worktrees/a", branch: "feat/a" },
    ]);
  });

  it("groups sessions without a known path by branch", () => {
    const targets = archiveWorktreeTargets([
      session("a", { git_branch: "feat/a", workspace: null }),
      session("b", { git_branch: "feat/a", workspace: "  " }),
      session("c", { git_branch: "feat/c" }),
    ]);

    expect(targets).toEqual([
      { path: null, branch: "feat/a" },
      { path: null, branch: "feat/c" },
    ]);
  });

  it("folds a pathless session into the worktree that has its branch", () => {
    const targets = archiveWorktreeTargets([
      session("a", { git_branch: "feat/a", workspace: null }),
      session("b", { git_branch: "feat/a", workspace: "/repo/.worktrees/a" }),
      session("c", { git_branch: "feat/c", workspace: null }),
    ]);

    expect(targets).toEqual([
      { path: "/repo/.worktrees/a", branch: "feat/a" },
      { path: null, branch: "feat/c" },
    ]);
  });
});
