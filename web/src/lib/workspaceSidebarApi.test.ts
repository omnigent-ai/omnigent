import { afterEach, describe, expect, it, vi } from "vitest";

const workspaceApi = vi.hoisted(() => ({
  listDirectoryViaWorkspaceApi: vi.fn(),
  readFileViaWorkspaceApi: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", () => workspaceApi);

import {
  ComparisonKind,
  DEFAULT_WORKSPACE_SIDEBAR_API,
  setWorkspaceSidebarApiForTests,
  type WorkspaceSidebarApi,
  workspaceSidebarApi,
} from "./workspaceSidebarApi";

const SESSION = "conv_api";

afterEach(() => {
  setWorkspaceSidebarApiForTests(null);
  vi.clearAllMocks();
});

describe("DEFAULT_WORKSPACE_SIDEBAR_API", () => {
  it("lists directories and reads files through the workspace API", async () => {
    const listed = { data: [], hasMore: true };
    workspaceApi.listDirectoryViaWorkspaceApi.mockResolvedValue(listed);
    const signal = new AbortController().signal;
    await expect(
      DEFAULT_WORKSPACE_SIDEBAR_API.listDirectory(SESSION, "src", 50, signal),
    ).resolves.toBe(listed);
    expect(workspaceApi.listDirectoryViaWorkspaceApi).toHaveBeenCalledWith(
      SESSION,
      "src",
      50,
      signal,
    );

    workspaceApi.readFileViaWorkspaceApi.mockResolvedValue(null);
    await expect(DEFAULT_WORKSPACE_SIDEBAR_API.readFile(SESSION, "a.ts", signal)).resolves.toBe(
      null,
    );
    expect(workspaceApi.readFileViaWorkspaceApi).toHaveBeenCalledWith(SESSION, "a.ts", signal);
  });

  it("serves no search or Git method yet, without sending anything", async () => {
    const api = DEFAULT_WORKSPACE_SIDEBAR_API;
    const comparison = { kind: ComparisonKind.BRANCH_POINT };
    const answers = await Promise.all([
      api.searchFiles(SESSION, "a", { path: "", include: "", exclude: "", limit: 10 }),
      api.searchText(SESSION, "a", {
        caseSensitive: false,
        regex: false,
        include: "",
        exclude: "",
      }),
      api.resolveWorkingCopyBase(SESSION, comparison),
      api.listWorkingCopyChanges(SESSION, comparison),
      api.readBaseFile(SESSION, { commit: "c", path: "a.ts", object: "o" }),
      api.readLinkText(SESSION, "link"),
      api.listRepositoryRefs(SESSION),
      api.listBranchCommits(SESSION, ""),
      api.resolveRepositoryContext(SESSION),
    ]);
    expect(answers).toEqual(Array(9).fill(null));
    expect(workspaceApi.listDirectoryViaWorkspaceApi).not.toHaveBeenCalled();
    expect(workspaceApi.readFileViaWorkspaceApi).not.toHaveBeenCalled();
  });
});

describe("setWorkspaceSidebarApiForTests", () => {
  it("swaps in a fake, and null restores the default", () => {
    expect(workspaceSidebarApi()).toBe(DEFAULT_WORKSPACE_SIDEBAR_API);
    const fake = { ...DEFAULT_WORKSPACE_SIDEBAR_API } satisfies WorkspaceSidebarApi;
    setWorkspaceSidebarApiForTests(fake);
    expect(workspaceSidebarApi()).toBe(fake);
    setWorkspaceSidebarApiForTests(null);
    expect(workspaceSidebarApi()).toBe(DEFAULT_WORKSPACE_SIDEBAR_API);
  });
});
