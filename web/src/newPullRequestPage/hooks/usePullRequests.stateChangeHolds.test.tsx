// A pull request's state changes through the session's host go one at a time: while a merge is
// being sent, a close of the same PR is refused as busy and sends nothing, until the merge settles.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as PullRequestRpcModule from "@/lib/pullRequestRpc";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";

const rpc = vi.hoisted(() => ({
  sent: [] as { method: string; resolve: (answer: unknown) => void }[],
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestRpcModule>()),
  writePullRequestRpc: (_conversationId: string, request: { method: string }) =>
    new Promise((resolve) => {
      rpc.sent.push({ method: request.method, resolve });
    }),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<typeof PageRouteModule>()),
  hostChangeRoute: (conversationId: string) => ({ conversationId, key: "route-1" }),
}));

import { resetPullRequestStateHoldsForTests } from "@/lib/pullRequestStateHolds";

import { useMergePullRequest, usePullRequestLifecycleChange } from "./usePullRequests";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "conv_1";

function renderHooks() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => ({
      merge: useMergePullRequest(SESSION),
      close: usePullRequestLifecycleChange(SESSION, "pull_request_close"),
    }),
    { wrapper },
  );
}

afterEach(() => {
  rpc.sent.length = 0;
  resetPullRequestStateHoldsForTests();
});

describe("state changes to one pull request", () => {
  it("refuses a close as busy while a merge is being sent, and sends it once the merge settles", async () => {
    const { result } = renderHooks();
    act(() =>
      result.current.merge.mutate({ prUrl: PR, mergeMethod: "squash", expectedHead: "abc" }),
    );
    await vi.waitFor(() =>
      expect(rpc.sent.map((sent) => sent.method)).toEqual(["mergePullRequest"]),
    );

    let busy: unknown;
    await act(async () => {
      busy = await result.current.close.change(PR);
    });
    expect(busy).toMatchObject({ kind: "refused", code: "busy" });
    expect(rpc.sent).toHaveLength(1);

    await act(async () => rpc.sent[0]!.resolve({ pr_url: PR }));
    let closed: Promise<unknown> | undefined;
    act(() => {
      closed = result.current.close.change(PR);
    });
    await vi.waitFor(() =>
      expect(rpc.sent.map((sent) => sent.method)).toEqual([
        "mergePullRequest",
        "setPullRequestState",
      ]),
    );
    await act(async () =>
      rpc.sent[1]!.resolve({ object: "session.github.pr_state", pr_url: PR, state: "CLOSED" }),
    );
    expect(await closed).toBeDefined();
  });
});
