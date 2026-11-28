// PR references and link previews without this Mac's shell: where the session's host serves
// LookupPullRequest to this caller (its real owner), the PR is looked up through it; a PR
// GitHub doesn't show reads as missing; elsewhere nothing is read.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ serves: true, read: vi.fn() }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (sessionId: string, method: string) =>
    host.serves && ["s1", "s2"].includes(sessionId) && method === "lookupPullRequest",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
}));

import { usePullRequestPreview } from "@/hooks/usePullRequestPreview";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { WorkspaceClient } from "@/lib/workspaceApi";

import { createPageLookupBudget } from "./lookupQueue";
import { PullRequestReferenceScopeContext, type PullRequestReferenceScopeValue } from "./scope";
import { useReferencedPullRequest } from "./useReferencedPullRequest";

const PR = "https://github.com/acme/app/pull/42";
const FOUND = {
  object: "session.github.pull_request_lookup",
  url: PR,
  number: 42,
  title: "Fix the parser",
  state: "OPEN",
  is_draft: false,
  repository: "acme/app",
  updated_at: "2026-11-24T10:00:00Z",
};

function wrapperFor(
  scope: PullRequestReferenceScopeValue | null,
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return ({ children }: { children: ReactNode }) =>
    createElement(
      QueryClientProvider,
      { client },
      createElement(PullRequestReferenceScopeContext.Provider, { value: scope }, children),
    );
}

const scopeFor = (hostSession?: string): PullRequestReferenceScopeValue => ({
  shorthand: () => null,
  link: () => null,
  budget: createPageLookupBudget(),
  ...(hostSession ? { hostSession } : {}),
});

beforeEach(() => {
  host.serves = true;
  host.read.mockReset();
});

describe("a PR reference through the session's host", () => {
  const target = parseCodeHostPullRequestUrl(PR);

  it("looks the PR up through LookupPullRequest and shows its title", async () => {
    host.read.mockResolvedValue(FOUND);
    const { result } = renderHook(() => useReferencedPullRequest(target, scopeFor("s1"), true), {
      wrapper: wrapperFor(scopeFor("s1")),
    });
    await waitFor(() => expect(result.current.status).toBe("found"));
    expect(result.current.pr?.title).toBe("Fix the parser");
    const [[session, method, call]] = host.read.mock.calls;
    expect([session, method]).toEqual(["s1", "lookupPullRequest"]);
    const lookupPullRequest = vi.fn(async () => ({ payload: {} }));
    await call({ lookupPullRequest } as unknown as WorkspaceClient);
    expect(lookupPullRequest).toHaveBeenCalledWith({ prUrl: PR });
  });

  it("asks another session's host itself, past one host's missing answer", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    host.read.mockImplementation(async (session: string) => {
      if (session === "s1") throw new PullRequestApiError("GitHub has no such pull request.", 400);
      return FOUND;
    });
    const first = renderHook(() => useReferencedPullRequest(target, scopeFor("s1"), true), {
      wrapper: wrapperFor(scopeFor("s1"), client),
    });
    await waitFor(() => expect(first.result.current.status).toBe("missing"));

    const second = renderHook(() => useReferencedPullRequest(target, scopeFor("s2"), true), {
      wrapper: wrapperFor(scopeFor("s2"), client),
    });
    await waitFor(() => expect(second.result.current.status).toBe("found"));
    expect(host.read.mock.calls.map(([session]) => session)).toEqual(["s1", "s2"]);
  });

  it("reads a PR the host says GitHub doesn't show as missing", async () => {
    host.read.mockRejectedValue(new PullRequestApiError("GitHub has no such pull request.", 400));
    const { result } = renderHook(() => useReferencedPullRequest(target, scopeFor("s1"), true), {
      wrapper: wrapperFor(scopeFor("s1")),
    });
    await waitFor(() => expect(result.current.status).toBe("missing"));
  });

  it("reads nothing without the host's method, or in a view with no session", () => {
    const noSession = renderHook(() => useReferencedPullRequest(target, scopeFor(), true), {
      wrapper: wrapperFor(scopeFor()),
    });
    expect(noSession.result.current.status).toBe("unread");
    host.serves = false;
    const unserved = renderHook(() => useReferencedPullRequest(target, scopeFor("s1"), true), {
      wrapper: wrapperFor(scopeFor("s1")),
    });
    expect(unserved.result.current.status).toBe("unread");
    expect(host.read).not.toHaveBeenCalled();
  });
});

describe("a link preview through the session's host", () => {
  it("previews a pasted PR link through LookupPullRequest", async () => {
    host.read.mockResolvedValue(FOUND);
    const { result } = renderHook(() => usePullRequestPreview(PR, true, "s1"), {
      wrapper: wrapperFor(null),
    });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current).toMatchObject({ preview: { title: "Fix the parser", state: "OPEN" } });
  });

  it("shows no preview, and reads nothing, without the host's method", () => {
    host.serves = false;
    const { result } = renderHook(() => usePullRequestPreview(PR, true, "s1"), {
      wrapper: wrapperFor(null),
    });
    expect(result.current.status).toBe("none");
    expect(host.read).not.toHaveBeenCalled();
  });
});
