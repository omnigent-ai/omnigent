// A capable host behind a server without the commit routes: a 404 from a commit read marks that
// PR's commits unsupported, and nothing else. Other read failures keep their own error.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(() => true),
  useSessionHostOnline: vi.fn(() => true),
}));
vi.mock("@/store/chatStore", () => ({ useChatStore: vi.fn() }));

import {
  PullRequestCommitsUnsupportedError,
  usePullRequestCommits,
  usePullRequestCommitsUnsupported,
} from "./usePullRequests";

const fetchMock = vi.fn();
const PR_URL = "https://github.com/org/repo/pull/1";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
  } as unknown as Response;
}

function renderCommits(conversationId: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => ({
      list: usePullRequestCommits(conversationId, PR_URL, "head1", true),
      unsupported: usePullRequestCommitsUnsupported(conversationId, PR_URL),
    }),
    { wrapper },
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("commit reads against an older server", () => {
  it("marks only the PR whose commit route answers 404 as unsupported, with a typed error", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { detail: "Not Found" }));

    const unsupported = renderCommits("conv-old-server");
    await waitFor(() =>
      expect(unsupported.result.current.list.error).toBeInstanceOf(
        PullRequestCommitsUnsupportedError,
      ),
    );
    expect(unsupported.result.current.unsupported).toBe(true);

    const other = renderCommits("conv-other-session");
    expect(other.result.current.unsupported).toBe(false);
  });

  it("keeps an ordinary read failure as its own error, and does not mark the PR", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(400, { detail: "Expected head_sha to be a full commit SHA" }),
    );

    const { result } = renderCommits("conv-read-failure");
    await waitFor(() => expect(result.current.list.error).not.toBeNull());

    expect(result.current.list.error).not.toBeInstanceOf(PullRequestCommitsUnsupportedError);
    expect(result.current.list.error?.message).toBe("Expected head_sha to be a full commit SHA");
    expect(result.current.unsupported).toBe(false);
  });
});
