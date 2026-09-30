import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

import { fetchConnectionBranches, fetchConnectionRepos } from "./connectionsApi";
import { fetchGithubBranches, fetchGithubRepos } from "./githubIntegration";
import { authenticatedFetch } from "./identity";

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

afterEach(() => vi.resetAllMocks());

function respondWith(body: unknown, init: { ok?: boolean; status?: number } = {}): void {
  authenticatedFetchMock.mockResolvedValue({
    ok: init.ok ?? true,
    status: init.status ?? 200,
    json: async () => body,
  } as unknown as Response);
}

describe("fetchConnectionRepos", () => {
  it("lists the repos at the provider's connection path", async () => {
    const list = {
      connected: true,
      repos: [
        {
          full_name: "octo/hello",
          clone_url: "https://github.com/octo/hello.git",
          default_branch: "main",
          private: false,
          pushed_at: "2026-07-28T00:00:00Z",
        },
      ],
      truncated: false,
    };
    respondWith(list);

    await expect(fetchConnectionRepos("github")).resolves.toEqual(list);
    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/connections/github/repos");
  });

  it("puts the provider id in the path", async () => {
    respondWith({ connected: false, repos: [] });

    await fetchConnectionRepos("azure_devops");

    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/connections/azure_devops/repos");
  });

  it("rejects with the provider and status when the list fails", async () => {
    respondWith({}, { ok: false, status: 502 });

    await expect(fetchConnectionRepos("github")).rejects.toThrow(
      "Connection repos failed (github): 502",
    );
  });
});

describe("fetchConnectionBranches", () => {
  it("lists the branches of a repo at the provider's connection path", async () => {
    respondWith({ connected: true, branches: ["main", "dev"] });

    await expect(fetchConnectionBranches("github", "octo/hello")).resolves.toEqual({
      connected: true,
      branches: ["main", "dev"],
    });
    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/v1/connections/github/repos/octo/hello/branches",
    );
  });

  it("encodes each segment of the repo name and keeps the slashes", async () => {
    respondWith({ connected: true, branches: [] });

    await fetchConnectionBranches("azure_devops", "My Org/proj 1/repo#1");

    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/v1/connections/azure_devops/repos/My%20Org/proj%201/repo%231/branches",
    );
  });

  it("rejects with the provider and status when the list fails", async () => {
    respondWith({}, { ok: false, status: 404 });

    await expect(fetchConnectionBranches("github", "octo/hello")).rejects.toThrow(
      "Connection branches failed (github): 404",
    );
  });
});

describe("deprecated GitHub wrappers", () => {
  it("fetchGithubRepos reads the GitHub connection's repos", async () => {
    respondWith({ connected: true, repos: [] });

    await expect(fetchGithubRepos()).resolves.toEqual({ connected: true, repos: [] });
    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/connections/github/repos");
  });

  it("fetchGithubBranches reads the GitHub connection's branches", async () => {
    respondWith({ connected: true, branches: ["main"] });

    await expect(fetchGithubBranches("octo/hello")).resolves.toEqual({
      connected: true,
      branches: ["main"],
    });
    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/v1/connections/github/repos/octo/hello/branches",
    );
  });
});
