import { Code, ConnectError } from "@connectrpc/connect";
import { afterEach, describe, expect, it, vi } from "vitest";

const workspaceApi = vi.hoisted(() => ({
  listDirectoryViaWorkspaceApi: vi.fn(),
  readFileViaWorkspaceApi: vi.fn(),
  searchFilesViaWorkspaceApi: vi.fn(),
  searchTextViaWorkspaceApi: vi.fn(),
  resolveWorkingCopyBaseViaWorkspaceApi: vi.fn(),
  listWorkingCopyChangesViaWorkspaceApi: vi.fn(),
  readBaseFileViaWorkspaceApi: vi.fn(),
  readLinkTextViaWorkspaceApi: vi.fn(),
  listRepositoryRefsViaWorkspaceApi: vi.fn(),
  listBranchCommitsViaWorkspaceApi: vi.fn(),
  resolveRepositoryContextViaWorkspaceApi: vi.fn(),
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

  it("searches file paths and contents through the workspace API", async () => {
    const signal = new AbortController().signal;
    const fileOptions = { path: "src", include: "*.ts", exclude: "", limit: 10 };
    const found = { data: [], hasMore: false, truncated: true };
    workspaceApi.searchFilesViaWorkspaceApi.mockResolvedValue(found);
    await expect(
      DEFAULT_WORKSPACE_SIDEBAR_API.searchFiles(SESSION, "app", fileOptions, signal),
    ).resolves.toBe(found);
    expect(workspaceApi.searchFilesViaWorkspaceApi).toHaveBeenCalledWith(
      SESSION,
      "app",
      fileOptions,
      signal,
    );

    const textOptions = { caseSensitive: true, regex: false, include: "", exclude: "dist/**" };
    // null: no route serves it, so the source searches through REST.
    workspaceApi.searchTextViaWorkspaceApi.mockResolvedValue(null);
    await expect(
      DEFAULT_WORKSPACE_SIDEBAR_API.searchText(SESSION, "todo", textOptions, signal),
    ).resolves.toBeNull();
    expect(workspaceApi.searchTextViaWorkspaceApi).toHaveBeenCalledWith(
      SESSION,
      "todo",
      textOptions,
      signal,
    );
  });

  const signal = new AbortController().signal;
  const comparison = { kind: ComparisonKind.REVISIONS, baseRevision: "main", headRevision: "" };
  const baseFile = { commit: "c".repeat(40), path: "a.ts", object: "o".repeat(40) };
  // Each Git method, the client function it calls, and its arguments after the session.
  const GIT_METHODS = [
    ["resolveWorkingCopyBase", "resolveWorkingCopyBaseViaWorkspaceApi", [comparison, signal]],
    ["listWorkingCopyChanges", "listWorkingCopyChangesViaWorkspaceApi", [comparison, signal]],
    ["readBaseFile", "readBaseFileViaWorkspaceApi", [baseFile, signal]],
    ["readLinkText", "readLinkTextViaWorkspaceApi", ["link", signal]],
    ["listRepositoryRefs", "listRepositoryRefsViaWorkspaceApi", [signal]],
    ["listBranchCommits", "listBranchCommitsViaWorkspaceApi", ["main", signal]],
    ["resolveRepositoryContext", "resolveRepositoryContextViaWorkspaceApi", [signal]],
  ] as const;

  it.each(GIT_METHODS)("reads %s through the workspace API", async (method, client, args) => {
    const answer = { from: client };
    workspaceApi[client].mockResolvedValue(answer);
    const call = DEFAULT_WORKSPACE_SIDEBAR_API[method] as (...a: unknown[]) => Promise<unknown>;

    await expect(call(SESSION, ...args)).resolves.toBe(answer);
    expect(workspaceApi[client]).toHaveBeenCalledWith(SESSION, ...args);
  });

  it.each(GIT_METHODS)(
    "passes on null and the host's refusal for %s",
    async (method, client, args) => {
      const call = DEFAULT_WORKSPACE_SIDEBAR_API[method] as (...a: unknown[]) => Promise<unknown>;
      // null: no route serves it, so the sidebar says it isn't available on this host.
      workspaceApi[client].mockResolvedValue(null);
      await expect(call(SESSION, ...args)).resolves.toBeNull();

      const refusal = new ConnectError("No revision 'main'", Code.NotFound);
      workspaceApi[client].mockRejectedValue(refusal);
      await expect(call(SESSION, ...args)).rejects.toBe(refusal);
    },
  );
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
