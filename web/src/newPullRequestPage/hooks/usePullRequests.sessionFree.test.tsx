// A session-free pull request view reads a PR only through the session that links it, and with
// none known asks nothing at all, so the view can say so plainly instead of failing.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => true,
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: unknown) =>
    typeof selector === "function"
      ? (selector as (state: { conversationId: null; sessionStatus: "idle" }) => unknown)({
          conversationId: null,
          sessionStatus: "idle",
        })
      : undefined,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

import {
  type SessionFreePullRequestScope,
  SessionFreePullRequestScopeContext,
} from "@/hooks/sessionFreePullRequestScope";

import { usePullRequestInfo } from "./usePullRequests";

const SCOPE = "pull-requests-page";
const PR = "https://github.com/acme/app/pull/7";
const fetchMock = vi.fn();

const INFO = {
  object: "session.github.info",
  available: true,
  provider: "github",
  selected_pr_url: PR,
  auth: { authenticated: true, hint: null, cli: null, accounts: [], selected_account: null },
  pr: { number: 7, title: "Linked elsewhere", state: "OPEN", url: PR, head_sha: "a".repeat(40) },
};

function read(scope: SessionFreePullRequestScope) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(() => usePullRequestInfo(SCOPE, { prUrl: PR }), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <SessionFreePullRequestScopeContext.Provider value={scope}>
          {children}
        </SessionFreePullRequestScopeContext.Provider>
      </QueryClientProvider>
    ),
  });
}

const requestedSessions = () =>
  fetchMock.mock.calls.map(([input]) => /\/v1\/sessions\/([^/]+)\//.exec(String(input))?.[1]);

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(new Response(JSON.stringify(INFO), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("asks nothing, and fails nothing, for a PR no known session links", async () => {
  const { result } = read({ id: SCOPE });
  await new Promise((resolve) => {
    setTimeout(resolve, 20);
  });
  expect(fetchMock).not.toHaveBeenCalled();
  expect(result.current.fetchStatus).toBe("idle");
  expect(result.current.error).toBeNull();
  expect(result.current.data).toBeUndefined();
});

it("reads a PR through the host of the session that links it", async () => {
  const { result } = read({
    id: SCOPE,
    linkingSessionOf: (url) => (url === PR ? { id: "conv_auth", name: "auth-fix" } : undefined),
  });
  await waitFor(() => expect(result.current.data?.pr?.title).toBe("Linked elsewhere"));
  expect(requestedSessions()).toEqual(["conv_auth"]);
});
