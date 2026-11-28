// The PR panel's checks poll reads fast again whenever its details start watching a PR, even
// on the same mounted panel: opened again after it rested, or after a list row showed it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

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

import { resetHostTransportRecovery } from "@/lib/hostTransportRecovery";
import { usePullRequestInfo } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/o/r/pull/1";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetHostTransportRecovery();
});

/** A PR with no checks yet, answered at once; returns when each read went out, in ms. */
function stubNoChecksPullRequest() {
  const started = Date.now();
  const readsAt: number[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      readsAt.push(Date.now() - started);
      return new Response(
        JSON.stringify({
          object: "session.github.info",
          available: true,
          authenticated: true,
          gh_available: true,
          branch: "feature",
          repo: { name_with_owner: "o/r" },
          base_ref: "main",
          pr: {
            number: 1,
            title: "t",
            state: "OPEN",
            url: PR_URL,
            is_draft: false,
            author: null,
            base_ref: "main",
            head_ref: "feature",
            head_sha: "a1",
            checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
          },
        }),
      );
    }),
  );
  return readsAt;
}

it.each([
  ["closed and opened again", false],
  ["shown as a list row, then opened", "list"],
] as const)("reads fast again when the same PR's details are %s", async (_scenario, resting) => {
  vi.useFakeTimers();
  const readsAt = stubNoChecksPullRequest();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const { rerender } = renderHook(
    ({ poll }: { poll: boolean | "list" }) =>
      usePullRequestInfo("session", { poll, prUrl: PR_URL }),
    { wrapper, initialProps: { poll: true as boolean | "list" } },
  );
  // Twelve minutes with no checks: the poll has slowed to every 2 minutes.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(12 * 60_000);
  });
  rerender({ poll: resting });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
  rerender({ poll: true });
  // The panel refreshes a stale PR as it opens.
  await act(async () => {
    await client.refetchQueries({ queryKey: ["pr-page-github-info", "session"], stale: true });
  });
  const reopenedAt = readsAt.at(-1)!;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30_000);
  });
  client.clear();
  const after = readsAt.filter((at) => at > reopenedAt);
  expect(after[0]! - reopenedAt).toBe(5_000);
});
