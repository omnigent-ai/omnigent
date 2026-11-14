import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useHosts } from "@/hooks/useHosts";
import { WORKTREE_ROOT_LABEL_KEY } from "@/lib/worktreeRoot";
import { conversation } from "@/test/sidebarMockHelpers";

import { ArchiveWorktreeList, archiveWorktreeTargets } from "./ArchivePrompt";

vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
const useHostsMock = vi.mocked(useHosts);

// Server `worktree_root_fingerprint` of "/repo/.worktrees/a" and "C:\\repo\\.worktrees\\a".
const ROOT_A_FINGERPRINT = "1a1329852b078cc01f5b390143afdd4bffbbaf52772d4b7c36ff8a435bfcf287";
const WINDOWS_ROOT_A_FINGERPRINT =
  "80ecddbd344246011666ff6a111ee0be2278a0b1ca388463f48dda5983de1a61";

const HOST = "host_1";

function onHost(id: string, fields: Parameters<typeof conversation>[1]) {
  return conversation(id, { host_id: HOST, ...fields });
}

describe("archiveWorktreeTargets", () => {
  it("lists each worktree once in selection order and skips sessions without one", () => {
    const targets = archiveWorktreeTargets([
      onHost("a", { git_branch: "fix/b", workspace: "/repo/.worktrees/b" }),
      onHost("b", { workspace: "/repo" }),
      onHost("c", { git_branch: "feat/a", workspace: "/repo/.worktrees/a" }),
      onHost("d", { git_branch: "fix/b", workspace: "/repo/.worktrees/b" }),
    ]);

    expect(targets).toEqual([
      { hostId: HOST, path: "/repo/.worktrees/b", branch: "fix/b" },
      { hostId: HOST, path: "/repo/.worktrees/a", branch: "feat/a" },
    ]);
  });

  it("names the recorded root of sessions in worktree subdirectories", () => {
    const labels = { [WORKTREE_ROOT_LABEL_KEY]: ROOT_A_FINGERPRINT };
    const targets = archiveWorktreeTargets([
      onHost("a", { git_branch: "feat/a", workspace: "/repo/.worktrees/a/web", labels }),
      onHost("b", { git_branch: "feat/a", workspace: "/repo/.worktrees/a/api", labels }),
      onHost("c", { git_branch: "feat/c", workspace: "/repo/elsewhere", labels }),
    ]);

    expect(targets).toEqual([
      { hostId: HOST, path: "/repo/.worktrees/a", branch: "feat/a" },
      { hostId: HOST, path: null, branch: "feat/c" },
    ]);
  });

  it("groups rootless sessions by branch unless that branch has a known root", () => {
    const targets = archiveWorktreeTargets([
      onHost("a", { git_branch: "feat/a", workspace: null }),
      onHost("b", { git_branch: "feat/a", workspace: "/repo/.worktrees/a" }),
      onHost("c", { git_branch: "feat/c", workspace: "  " }),
      onHost("d", { git_branch: "feat/c" }),
      // Cleanup needs a host, so a hostless session has no root either.
      conversation("e", { git_branch: "feat/e", workspace: "/repo/.worktrees/e" }),
    ]);

    expect(targets).toEqual([
      { hostId: HOST, path: "/repo/.worktrees/a", branch: "feat/a" },
      { hostId: HOST, path: null, branch: "feat/c" },
      { hostId: null, path: null, branch: "feat/e" },
    ]);
  });

  it("lists one worktree once however its Windows root is spelled", () => {
    const labels = { [WORKTREE_ROOT_LABEL_KEY]: WINDOWS_ROOT_A_FINGERPRINT };
    const targets = archiveWorktreeTargets([
      onHost("a", { git_branch: "feat/a", workspace: "C:/repo/.worktrees/a/web", labels }),
      onHost("b", {
        git_branch: "feat/a",
        workspace: "c:\\repo\\.worktrees\\a\\api",
        labels,
      }),
    ]);

    expect(targets).toEqual([{ hostId: HOST, path: "C:\\repo\\.worktrees\\a", branch: "feat/a" }]);
  });

  it("keeps the same path on different hosts apart", () => {
    const targets = archiveWorktreeTargets([
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
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host_1", name: "Build box" }],
    } as unknown as ReturnType<typeof useHosts>);
    const { rerender } = render(
      <ArchiveWorktreeList worktrees={[{ hostId: "host_1", path: "/repo/wt", branch: "a" }]} />,
    );
    expect(screen.getByTestId("archive-worktree-name")).toHaveTextContent(/^wt$/);

    rerender(
      <ArchiveWorktreeList
        worktrees={[{ hostId: null, path: "\\\\srv\\share\\wt", branch: "a" }]}
      />,
    );
    expect(screen.getByTestId("archive-worktree-name")).toHaveTextContent(/^wt$/);
    expect(screen.queryByTestId("archive-worktree-host")).not.toBeInTheDocument();

    // A host missing from the host list falls back to its id; a hostless target says so.
    rerender(
      <ArchiveWorktreeList
        worktrees={[
          { hostId: "host_1", path: "/repo/wt", branch: "a" },
          { hostId: "host_2", path: "/repo/wt", branch: "b" },
          { hostId: null, path: null, branch: "c" },
        ]}
      />,
    );
    expect(screen.getAllByTestId("archive-worktree-host").map((host) => host.textContent)).toEqual([
      "Build box",
      "host_2",
      "No host",
    ]);
  });
});
