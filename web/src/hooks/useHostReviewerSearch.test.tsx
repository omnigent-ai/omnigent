// Reviewer search through the session's host: offered only where the host serves
// SearchReviewers for a GitHub PR; what it finds is listed ahead of what past searches kept.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ serves: true, read: vi.fn() }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) =>
    host.serves && method === "searchReviewers",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/lib/durableServerScope", () => ({ durableServerScope: () => "server-a" }));

import type { WorkspaceClient } from "@/lib/workspaceApi";
import {
  rememberReviewerSearch,
  resetReviewerSearchCacheForTests,
} from "@/lib/reviewerSearchCache";

import {
  parseReviewerSearch,
  useHostReviewerSearch,
  useHostReviewerSearchOffered,
} from "./useHostReviewerSearch";

const PR = "https://github.com/acme/app/pull/42";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  host.serves = true;
  host.read.mockReset();
  resetReviewerSearchCacheForTests();
});
afterEach(() => resetReviewerSearchCacheForTests());

describe("useHostReviewerSearchOffered", () => {
  it("is offered on a GitHub PR the host searches", () => {
    const offered = () =>
      renderHook(() => useHostReviewerSearchOffered("s1", PR), { wrapper }).result.current;
    expect(offered()).toBe(true);
    host.serves = false;
    expect(offered()).toBe(false);
  });
});

describe("useHostReviewerSearch", () => {
  it("searches what's typed and lists its finds ahead of the kept ones, once each", async () => {
    rememberReviewerSearch("github.com", "acme/app", {
      people: [
        { login: "kept", name: null },
        { login: "Octo", name: null },
      ],
      teams: [],
    });
    host.read.mockResolvedValue({
      object: "list",
      users: [{ login: "octo", name: "Octo Cat" }],
      teams: [{ slug: "core", name: "Core" }],
    });
    const { result } = renderHook(() => useHostReviewerSearch("s1", PR, "oc", true), {
      wrapper,
    });
    // Kept ones show before the search answers.
    expect(result.current.candidates.map(({ login }) => login)).toEqual(["kept", "Octo"]);
    await waitFor(() => expect(result.current.searching).toBe(false), { timeout: 2000 });
    expect(result.current.candidates).toEqual([
      { kind: "user", login: "octo", name: "Octo Cat" },
      { kind: "team", login: "core", name: "Core" },
      { kind: "user", login: "kept", name: null },
    ]);
    const [[session, method, call]] = host.read.mock.calls;
    expect([session, method]).toEqual(["s1", "searchReviewers"]);
    const searchReviewers = vi.fn(async () => ({ payload: {} }));
    await call({ searchReviewers } as unknown as WorkspaceClient);
    expect(searchReviewers).toHaveBeenCalledWith({ prUrl: PR, text: "oc", pageSize: 20 });
  });

  it("reads nothing while closed or where the host doesn't search", async () => {
    renderHook(() => useHostReviewerSearch("s1", PR, "oc", false), { wrapper });
    host.serves = false;
    const { result } = renderHook(() => useHostReviewerSearch("s1", PR, "oc", true), {
      wrapper,
    });
    await new Promise((resolve) => {
      setTimeout(resolve, 300);
    });
    expect(host.read).not.toHaveBeenCalled();
    expect(result.current).toEqual({ candidates: [], searching: false });
  });
});

describe("parseReviewerSearch", () => {
  it("keeps well-formed people and teams and refuses an answer without users", () => {
    expect(parseReviewerSearch({ teams: [] })).toBeNull();
    expect(
      parseReviewerSearch({
        users: [{ login: "octo" }, { login: 3 }, null],
        teams: [{ slug: "core", name: 1 }],
      }),
    ).toEqual({ people: [{ login: "octo", name: null }], teams: [{ slug: "core", name: null }] });
  });
});
