// The pending review read on this Mac: only as the account the page writes as, in that account's
// own key, never shown once that account, sign-in, scope or workspace user moved on, and never
// taken as "no review" when it can't be read.

import type * as Identity from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { observedReadPriority } from "@/hooks/pullRequestReadPriority";
import { SecondaryReadPriorityProvider } from "@/hooks/secondaryReadPriority";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const workspaceUser = vi.hoisted(() => ({ id: "user-1" }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof Identity>()),
  getCurrentUserId: () => workspaceUser.id,
  subscribeIdentity: () => () => {},
}));

import {
  macWriteRoute,
  type MacWriteRoute,
  PENDING_REVIEW_READ,
  PENDING_REVIEW_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setPendingReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";
import {
  noReviewAnswer,
  pendingAnswer,
  REVIEW_ID,
  unplacedDraft,
} from "@/test/pendingReviewAnswers";

import { pendingReviewKey, usePendingReview } from "./usePendingReview";

const PR = "https://github.com/acme/app/pull/42";

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

/** A shell offering pending reviews, whose read answers `answers[login]` for that login. */
async function pendingShell(answers: Record<string, unknown>, asBob = true) {
  const shell = installNamedWriteShell(undefined, {
    extraWrites: [...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ],
    reads: { [PENDING_REVIEW_READ]: (login) => answers[login] ?? null },
  });
  await shell.ready(PR, { asBob });
  setPendingReviewsFlagForTests(true);
  return shell;
}

const pendingReads = (shell: Awaited<ReturnType<typeof pendingShell>>) =>
  shell.codeHostQuery.mock.calls.filter(([request]) => request.operation === PENDING_REVIEW_READ);

afterEach(() => {
  workspaceUser.id = "user-1";
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("the pending review's place among reads", () => {
  it.each(["prefetch", "interactive"] as const)(
    "reads at the priority the page's secondary reads want: %s",
    async (priority) => {
      await pendingShell({ alice: noReviewAnswer() }, false);
      const client = newClient();
      const { result } = renderHook(() => usePendingReview(PR, null), {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={client}>
            <SecondaryReadPriorityProvider value={priority}>
              {children}
            </SecondaryReadPriorityProvider>
          </QueryClientProvider>
        ),
      });
      await waitFor(() => expect(result.current.state.kind).toBe("none"));
      const [watched] = client
        .getQueryCache()
        .getAll()
        .filter(
          (query) => query.observers.length > 0 && query.queryKey[0] === "github-pending-review",
        );
      expect(observedReadPriority(client, watched!.queryKey)).toBe(priority);
    },
  );
});

describe("usePendingReview", () => {
  it("is unavailable, and reads nothing, where pending reviews aren't offered", async () => {
    const shell = await pendingShell({ alice: pendingAnswer() }, false);
    setPendingReviewsFlagForTests(false);
    const { result } = renderHook(() => usePendingReview(PR, null), {
      wrapper: wrapper(newClient()),
    });
    expect(result.current.state).toEqual({ kind: "unavailable" });
    expect(result.current.route).toBeNull();
    expect(pendingReads(shell)).toEqual([]);
  });

  it("reads the host account's pending review as that account", async () => {
    const shell = await pendingShell(
      { alice: pendingAnswer({ unplaced: [unplacedDraft("PRRC_1")] }) },
      false,
    );
    const { result } = renderHook(() => usePendingReview(PR, null), {
      wrapper: wrapper(newClient()),
    });
    expect(result.current.state).toEqual({ kind: "loading" });
    await waitFor(() => expect(result.current.state.kind).toBe("pending"));
    expect(result.current.state).toMatchObject({
      review: { id: REVIEW_ID },
      complete: true,
      stale: false,
    });
    expect(pendingReads(shell)[0]![0]).not.toHaveProperty("macLogin");
  });

  it("reads a named login's own pending review by name, never the host account's", async () => {
    const shell = await pendingShell({
      alice: pendingAnswer({ reviewId: "PRR_alice" }),
      bob: noReviewAnswer(),
    });
    const { result } = renderHook(() => usePendingReview(PR, BOB), {
      wrapper: wrapper(newClient()),
    });
    await waitFor(() => expect(result.current.state.kind).toBe("none"));
    expect(pendingReads(shell)[0]![0]).toMatchObject({ macLogin: "bob" });
  });

  it("is an error, never none, for an answer it can't read, as an older shell's", async () => {
    const { read_count: _read, ...olderReview } = pendingAnswer().review;
    await pendingShell({ alice: { ...pendingAnswer(), review: olderReview } }, false);
    const { result } = renderHook(() => usePendingReview(PR, null), {
      wrapper: wrapper(newClient()),
    });
    await waitFor(() => expect(result.current.state).toEqual({ kind: "error" }));
  });

  it("drops a read answered after the login signed in again, and reads under the new sign-in", async () => {
    const shell = await pendingShell({ bob: pendingAnswer() });
    const paused = shell.pauseNextRead(PENDING_REVIEW_READ);
    const client = newClient();
    const before = macWriteRoute(PR, BOB)!;
    const { result } = renderHook(() => usePendingReview(PR, BOB), { wrapper: wrapper(client) });
    await paused.started;
    shell.bobSignsInAgain(2);
    await shell.readAsBob(PR);
    paused.release();
    // The new sign-in's key reads again and shows only its own answer.
    await waitFor(() => expect(result.current.state.kind).toBe("pending"));
    expect(result.current.route?.actor.generation).toBe(2);
    // The read sent under the old sign-in answered nothing into its key.
    expect(client.getQueryData(pendingReviewKey(PR, before, "user-1"))).toBeUndefined();
  });

  it("keeps the last good read, marked stale, when a refetch fails", async () => {
    const answers: Record<string, unknown> = { alice: noReviewAnswer() };
    await pendingShell(answers, false);
    const client = newClient();
    const { result } = renderHook(() => usePendingReview(PR, null), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.state).toMatchObject({ kind: "none", stale: false }));
    answers.alice = { object: "session.github.pr_pending_review" };
    result.current.refetch();
    await waitFor(() => expect(result.current.state).toMatchObject({ kind: "none", stale: true }));
  });
});

describe("pendingReviewKey", () => {
  const route = (reader: MacWriteRoute["reader"], generation = 1): MacWriteRoute => ({
    target: { ...macWriteRouteTarget(), ...(reader ? { readAs: reader.login } : {}) },
    actor: { login: "alice", generation },
    reader,
  });
  function macWriteRouteTarget() {
    return {
      provider: "github" as const,
      host: "github.com",
      repository: "acme/app",
      number: 42,
      url: PR,
    } as MacWriteRoute["target"];
  }

  it("keeps the same login's drafts apart in the host's scope and by name", () => {
    const host = pendingReviewKey(PR, route(null), "user-1");
    const named = pendingReviewKey(PR, route({ host: "github.com", login: "alice" }), "user-1");
    expect(host).not.toEqual(named);
  });

  it("moves with the sign-in and the workspace user", () => {
    const first = pendingReviewKey(PR, route(null, 1), "user-1");
    expect(pendingReviewKey(PR, route(null, 2), "user-1")).not.toEqual(first);
    expect(pendingReviewKey(PR, route(null, 1), "user-2")).not.toEqual(first);
  });

  it("is shared by any spelling of the PR's URL", () => {
    expect(pendingReviewKey("https://github.com/ACME/app/pull/42", route(null), "user-1")).toEqual(
      pendingReviewKey(PR, route(null), "user-1"),
    );
  });
});
