// `#` completions: where the session's host serves SearchCompletions,
// the editor searches the PR's repository through it, asking for references only.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CompletionKind } from "@/gen/omnigent/api/workspace/v1alpha1/workspace_pb";

const host = vi.hoisted(() => ({ serves: true, read: vi.fn() }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (sessionId: string, method: string) =>
    host.serves && sessionId !== "" && method === "searchCompletions",
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
}));

import type { WorkspaceClient } from "@/lib/workspaceApi";

import { usePullRequestCompletions } from "./pullRequestCompletionSearch";

const PR = "https://github.com/acme/app/pull/1";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  host.serves = true;
  host.read.mockReset();
});

describe("# completions through the session's host", () => {
  it("searches the PR's repository for references and lists well-formed answers", async () => {
    host.read.mockResolvedValue({
      object: "list",
      kind: "reference",
      pull_requests: [
        {
          url: "https://github.com/acme/app/pull/7",
          number: 7,
          title: "Fix",
          state: "OPEN",
          is_draft: false,
        },
        { url: "https://github.com/acme/app/pull/8", number: "8", title: "Bad" },
      ],
    });
    const { result } = renderHook(() => usePullRequestCompletions(PR, "parser", "s1"), {
      wrapper,
    });
    await waitFor(() => expect(result.current.pullRequests).toHaveLength(1), { timeout: 2000 });
    expect(result.current.pullRequests[0]).toMatchObject({ number: 7, title: "Fix" });
    const [[session, method, call]] = host.read.mock.calls;
    expect([session, method]).toEqual(["s1", "searchCompletions"]);
    const searchCompletions = vi.fn(async () => ({ payload: {} }));
    await call({ searchCompletions } as unknown as WorkspaceClient);
    expect(searchCompletions).toHaveBeenCalledWith({
      prUrl: PR,
      kind: CompletionKind.REFERENCE,
      text: "parser",
    });
  });

  it("searches nothing where the host doesn't serve it, or for search syntax", async () => {
    host.serves = false;
    renderHook(() => usePullRequestCompletions(PR, "parser", "s1"), { wrapper });
    host.serves = true;
    renderHook(() => usePullRequestCompletions(PR, "repo:other/secret", "s1"), { wrapper });
    renderHook(() => usePullRequestCompletions(PR, "parser", ""), { wrapper });
    await new Promise((resolve) => {
      setTimeout(resolve, 400);
    });
    expect(host.read).not.toHaveBeenCalled();
  });
});
