// A pull request's state changes through the session's host go one at a time: while a merge is
// being sent, a close of the same PR is refused as busy and sends nothing, until the merge settles.
// A close GitHub may have made holds a merge until Check Again's fresh read says where it stands.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as PullRequestRpcModule from "@/lib/pullRequestRpc";
import type * as PageRouteModule from "@/pullRequestPage/pullRequestPageRoute";

const rpc = vi.hoisted(() => ({
  sent: [] as {
    method: string;
    resolve: (answer: unknown) => void;
    reject: (error: Error) => void;
  }[],
  infoReads: [] as { resolve: (info: unknown) => void; reject: (error: Error) => void }[],
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<typeof PullRequestRpcModule>()),
  writePullRequestRpc: (_conversationId: string, request: { method: string }) =>
    new Promise((resolve, reject) => {
      rpc.sent.push({ method: request.method, resolve, reject });
    }),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<typeof PageRouteModule>()),
  hostChangeRoute: (conversationId: string) => ({ conversationId, key: "route-1" }),
  hostChangeRouteCurrent: () => true,
}));
vi.mock("./usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  fetchPullRequestInfo: () =>
    new Promise((resolve, reject) => {
      rpc.infoReads.push({ resolve, reject });
    }),
}));

import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
import { resetPullRequestStateHoldsForTests } from "@/lib/pullRequestStateHolds";
import { readFreshPullRequest } from "@/pullRequestPage/freshPullRequestRead";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import {
  resetStateChainsForTests,
  stateChainKey,
  stateChainState,
} from "@/pullRequestPage/pullRequestStateChain";
import {
  captureStateChange,
  checkStateChangeAgain,
  startStateChange,
  useStateChangeSend,
} from "@/pullRequestPage/usePullRequestStateAction";

import { useMergePullRequest, usePullRequestLifecycleChange } from "./usePullRequests";

const PR = "https://github.com/acme/app/pull/42";
const ENTERPRISE_PR = "https://ghe.example.com/acme/app/pull/42";
const SESSION = "conv_1";
const OTHER_SESSION = "conv_2";

function renderHooks() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = renderHook(
    () => ({
      merge: useMergePullRequest(SESSION),
      close: usePullRequestLifecycleChange(SESSION, "pull_request_close"),
      send: useStateChangeSend(SESSION),
      closeFromOtherSession: usePullRequestLifecycleChange(OTHER_SESSION, "pull_request_close"),
    }),
    { wrapper },
  );
  return { ...view, client };
}

afterEach(() => {
  rpc.sent.length = 0;
  rpc.infoReads.length = 0;
  resetPullRequestStateHoldsForTests();
  resetStateChainsForTests();
});

describe("state changes to one pull request", () => {
  it.each([
    ["github.com", PR],
    ["a GitHub Enterprise host", ENTERPRISE_PR],
  ])(
    "refuses a close from another session as busy while a merge on %s is being sent, and sends it once the merge settles",
    async (_host, prUrl) => {
      const { result } = renderHooks();
      act(() => result.current.merge.mutate({ prUrl, mergeMethod: "squash", expectedHead: "abc" }));
      await vi.waitFor(() =>
        expect(rpc.sent.map((sent) => sent.method)).toEqual(["mergePullRequest"]),
      );

      let busy: unknown;
      await act(async () => {
        busy = await result.current.closeFromOtherSession.change(prUrl);
      });
      expect(busy).toMatchObject({ kind: "refused", code: "busy" });
      expect(rpc.sent).toHaveLength(1);

      await act(async () => rpc.sent[0]!.resolve({ pr_url: prUrl }));
      let closed: Promise<unknown> | undefined;
      act(() => {
        closed = result.current.closeFromOtherSession.change(prUrl);
      });
      await vi.waitFor(() =>
        expect(rpc.sent.map((sent) => sent.method)).toEqual([
          "mergePullRequest",
          "setPullRequestState",
        ]),
      );
      await act(async () =>
        rpc.sent[1]!.resolve({ object: "session.github.pr_state", pr_url: prUrl, state: "CLOSED" }),
      );
      expect(await closed).toBeDefined();
    },
  );

  it("holds the PR after a lost merge answer, until a fresh read shows where it stands", async () => {
    const { result, client } = renderHooks();
    act(() =>
      result.current.merge.mutate({ prUrl: PR, mergeMethod: "squash", expectedHead: "abc" }),
    );
    await vi.waitFor(() =>
      expect(rpc.sent.map((sent) => sent.method)).toEqual(["mergePullRequest"]),
    );
    // The merge may have landed: its answer is lost.
    await act(async () => rpc.sent[0]!.reject(new PullRequestMaybeSentError(new Error("dropped"))));
    await vi.waitFor(() => expect(client.isMutating()).toBe(0));

    // Another merge or a close is refused, with nothing sent, while the hold stands.
    const refusedWhileHeld = async () => {
      const onError = vi.fn();
      act(() =>
        result.current.merge.mutate(
          { prUrl: PR, mergeMethod: "squash", expectedHead: "abc" },
          { onError },
        ),
      );
      expect(onError).toHaveBeenCalledWith(
        expect.objectContaining({ message: expect.stringMatching(/Check it again/) }),
        expect.anything(),
      );
      let closed: unknown;
      await act(async () => {
        closed = await result.current.close.change(PR);
      });
      expect(closed).toMatchObject({
        kind: "refused",
        message: expect.stringMatching(/Check it again/),
      });
      expect(rpc.sent).toHaveLength(1);
    };
    await refusedWhileHeld();

    // While the confirming read is pending, and after it fails, the hold stays.
    const checking = readFreshPullRequest(client, SESSION, PR);
    await vi.waitFor(() => expect(rpc.infoReads).toHaveLength(1));
    await refusedWhileHeld();
    rpc.infoReads[0]!.reject(new Error("offline"));
    await expect(checking).resolves.toMatchObject({ result: "failed" });
    await refusedWhileHeld();

    // A read that shows the PR's state settles the lost merge; the merge is not sent again.
    const confirmed = readFreshPullRequest(client, SESSION, PR);
    await vi.waitFor(() => expect(rpc.infoReads).toHaveLength(2));
    rpc.infoReads[1]!.resolve({ selected_pr_url: PR, pr: { url: PR, state: "OPEN" } });
    await expect(confirmed).resolves.toMatchObject({ result: "confirmed" });
    act(() =>
      result.current.merge.mutate({ prUrl: PR, mergeMethod: "squash", expectedHead: "abc" }),
    );
    await vi.waitFor(() =>
      expect(rpc.sent.map((sent) => sent.method)).toEqual(["mergePullRequest", "mergePullRequest"]),
    );
  });

  it("holds a merge after a close GitHub may have made, until Check Again confirms it", async () => {
    const { result, client } = renderHooks();
    const owner = draftOwner();
    const key = stateChainKey(owner, PR);
    const capture = captureStateChange(PR, owner, "pull_request_close", SESSION)!;
    let closing: Promise<unknown> | undefined;
    act(() => {
      closing = startStateChange({ key, owner, capture, send: result.current.send, origin: null });
    });
    await vi.waitFor(() =>
      expect(rpc.sent.map((sent) => sent.method)).toEqual(["setPullRequestState"]),
    );
    // An answer that doesn't say the state: GitHub may have closed it.
    await act(async () => rpc.sent[0]!.resolve({}));
    await closing;
    expect(stateChainState(key).notice?.outcome.kind).toBe("unknown");

    const mergeRefused = () => {
      const onError = vi.fn();
      act(() =>
        result.current.merge.mutate(
          { prUrl: PR, mergeMethod: "squash", expectedHead: "abc" },
          { onError },
        ),
      );
      expect(onError).toHaveBeenCalledWith(
        expect.objectContaining({ message: expect.stringMatching(/Check it again/) }),
        expect.anything(),
      );
      expect(rpc.sent).toHaveLength(1);
    };
    mergeRefused();

    // While Check Again's read is pending, and after it fails, the merge still waits.
    const checking = checkStateChangeAgain(client, SESSION, key, PR);
    await vi.waitFor(() => expect(rpc.infoReads).toHaveLength(1));
    mergeRefused();
    rpc.infoReads[0]!.reject(new Error("offline"));
    await expect(checking).resolves.toBe("failed");
    mergeRefused();

    const confirmed = checkStateChangeAgain(client, SESSION, key, PR);
    await vi.waitFor(() => expect(rpc.infoReads).toHaveLength(2));
    rpc.infoReads[1]!.resolve({ selected_pr_url: PR, pr: { url: PR, state: "OPEN" } });
    await expect(confirmed).resolves.toBe("confirmed");
    act(() =>
      result.current.merge.mutate({ prUrl: PR, mergeMethod: "squash", expectedHead: "abc" }),
    );
    await vi.waitFor(() =>
      expect(rpc.sent.map((sent) => sent.method)).toEqual([
        "setPullRequestState",
        "mergePullRequest",
      ]),
    );
  });
});
