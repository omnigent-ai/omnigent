import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SessionWorktreesResult } from "@/hooks/useSessionWorktrees";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { SessionOverview } from "./SessionOverview";

const useSessionWorktreesMock = vi.fn();
const useWorkspaceChangedFilesMock = vi.fn();

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { hostId: "host-1", workspace: "/repo" } }),
}));
vi.mock("@/hooks/useSessionWorktrees", () => ({
  useSessionWorktrees: (...args: unknown[]) => useSessionWorktreesMock(...args),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: (id: string) => useWorkspaceChangedFilesMock(id),
}));
vi.mock("./SessionOverviewSubagents", () => ({ SessionOverviewSubagents: () => null }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const GIT: SessionWorktreesResult = {
  status: "ok",
  worktrees: [{ path: "/repo", branch: "main", is_main: true, detached: false }],
};

function file(
  path: string,
  status: WorkspaceChangedFile["status"],
  linesAdded: number | null,
  linesRemoved: number | null,
): WorkspaceChangedFile {
  return {
    path,
    name: path,
    status,
    bytes: null,
    modified_at: null,
    lines_added: linesAdded,
    lines_removed: linesRemoved,
  };
}

const tracked = file("edited.py", "modified", 2, 1);
const deleted = file("gone.py", "deleted", 0, 3);
const untracked = file("new.py", "created", null, null);

function rowText(label: string): string | null {
  return screen.queryByText(label)?.parentElement?.textContent ?? null;
}

describe("SessionOverviewChanges", () => {
  it.each([
    ["sums tracked lines", GIT, [tracked, deleted, untracked], "Files changed3", "Lines+2 −4"],
    ["hides unknown lines", GIT, [untracked], "Files changed1", null],
    ["hides a clean git workspace", GIT, [], null, null],
    ["hides a non-git workspace", { status: "not_git" }, [tracked], null, null],
    ["hides a loading workspace", undefined, [tracked], null, null],
    ["hides an unknown workspace", { status: "unknown" }, [tracked], null, null],
    ["hides an empty worktree list", { status: "ok", worktrees: [] }, [tracked], null, null],
  ] as const)("%s", (_, worktrees, files, filesRow, linesRow) => {
    useSessionWorktreesMock.mockReturnValue({ data: worktrees });
    useWorkspaceChangedFilesMock.mockReturnValue({ data: { available: true, data: files } });
    render(<SessionOverview onClose={() => {}} changesSessionId="conv_1" />);

    expect(rowText("Files changed")).toBe(filesRow);
    expect(rowText("Lines")).toBe(linesRow);
    expect(useSessionWorktreesMock).toHaveBeenCalledWith("conv_1", "host-1", "/repo");
    expect(useWorkspaceChangedFilesMock).toHaveBeenCalledWith("conv_1");
  });

  it("reads nothing when the workspace isn't browsable", () => {
    render(<SessionOverview onClose={() => {}} />);

    expect(screen.queryByText("Files changed")).toBeNull();
    expect(useSessionWorktreesMock).not.toHaveBeenCalled();
    expect(useWorkspaceChangedFilesMock).not.toHaveBeenCalled();
  });
});
