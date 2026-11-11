import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WORKTREE_ROOT_LABEL_KEY } from "@/lib/worktreeRoot";
import { conversation } from "@/test/sidebarMockHelpers";

import { ArchiveWorktreeList, archiveWorktreeTargets } from "./ArchivePrompt";

// Server `worktree_root_fingerprint("/repo/.worktrees/a")`.
const ROOT_A_FINGERPRINT = "1a1329852b078cc01f5b390143afdd4bffbbaf52772d4b7c36ff8a435bfcf287";

describe("archiveWorktreeTargets", () => {
  it("lists each worktree once in selection order and skips sessions without one", async () => {
    const targets = await archiveWorktreeTargets([
      conversation("a", { git_branch: "fix/b", workspace: "/repo/.worktrees/b" }),
      conversation("b", { workspace: "/repo" }),
      conversation("c", { git_branch: "feat/a", workspace: "/repo/.worktrees/a" }),
      conversation("d", { git_branch: "fix/b", workspace: "/repo/.worktrees/b" }),
    ]);

    expect(targets).toEqual([
      { hostId: null, path: "/repo/.worktrees/b", branch: "fix/b" },
      { hostId: null, path: "/repo/.worktrees/a", branch: "feat/a" },
    ]);
  });

  it("names the recorded root of sessions in worktree subdirectories", async () => {
    const labels = { [WORKTREE_ROOT_LABEL_KEY]: ROOT_A_FINGERPRINT };
    const targets = await archiveWorktreeTargets([
      conversation("a", { git_branch: "feat/a", workspace: "/repo/.worktrees/a/web", labels }),
      conversation("b", { git_branch: "feat/a", workspace: "/repo/.worktrees/a/api", labels }),
      conversation("c", { git_branch: "feat/c", workspace: "/repo/elsewhere", labels }),
    ]);

    expect(targets).toEqual([
      { hostId: null, path: "/repo/.worktrees/a", branch: "feat/a" },
      { hostId: null, path: null, branch: "feat/c" },
    ]);
  });

  it("groups rootless sessions by branch unless that branch has a known root", async () => {
    const targets = await archiveWorktreeTargets([
      conversation("a", { git_branch: "feat/a", workspace: null }),
      conversation("b", { git_branch: "feat/a", workspace: "/repo/.worktrees/a" }),
      conversation("c", { git_branch: "feat/c", workspace: "  " }),
      conversation("d", { git_branch: "feat/c" }),
    ]);

    expect(targets).toEqual([
      { hostId: null, path: "/repo/.worktrees/a", branch: "feat/a" },
      { hostId: null, path: null, branch: "feat/c" },
    ]);
  });

  it("keeps the same path on different hosts apart", async () => {
    const targets = await archiveWorktreeTargets([
      conversation("a", { host_id: "host_1", git_branch: "feat/a", workspace: "/repo/wt" }),
      conversation("b", { host_id: "host_2", git_branch: "feat/b", workspace: "/repo/wt" }),
      conversation("c", { host_id: "host_2", git_branch: "feat/a" }),
    ]);

    expect(targets).toEqual([
      { hostId: "host_1", path: "/repo/wt", branch: "feat/a" },
      { hostId: "host_2", path: "/repo/wt", branch: "feat/b" },
      { hostId: "host_2", path: null, branch: "feat/a" },
    ]);
  });
});

describe("ArchiveWorktreeList", () => {
  it("names each worktree, and its host only when the targets span hosts", () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { enabled: false } } });
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <ArchiveWorktreeList worktrees={[{ hostId: "host_1", path: "/repo/wt", branch: "a" }]} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("archive-worktree-name")).toHaveTextContent(/^wt$/);
    expect(screen.queryByTestId("archive-worktree-host")).not.toBeInTheDocument();

    rerender(
      <QueryClientProvider client={queryClient}>
        <ArchiveWorktreeList
          worktrees={[
            { hostId: "host_1", path: "/repo/wt", branch: "a" },
            { hostId: "host_2", path: "/repo/wt", branch: "b" },
          ]}
        />
      </QueryClientProvider>,
    );
    expect(screen.getAllByTestId("archive-worktree-host").map((host) => host.textContent)).toEqual([
      "host_1",
      "host_2",
    ]);
  });
});
