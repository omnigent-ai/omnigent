// Comments, replies and new conversations on the Mac write path, with the real
// read-path and write-path stores over a stand-in shell: which path a write takes,
// and which caches its answer may touch once it finishes.

import { invalidatedKey } from "@/test/invalidatedQueries";
import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { onlineManager, QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, hostFetch, toastSuccess, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  hostFetch: vi.fn(),
  toastSuccess: vi.fn(),
  identityListeners: new Set<() => void>(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: toastSuccess, error: vi.fn(), dismiss: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
  authenticatedFetch: hostFetch,
}));

import { usePullRequestStateAction } from "@/pullRequestPage/usePullRequestStateAction";
import { resetStateChainsForTests } from "@/pullRequestPage/pullRequestStateChain";
import {
  GITHUB_WRITE_SET_V1,
  LIFECYCLE_WRITE_OPERATIONS,
  setLifecycleFlagForTests,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
  setPageWriteOperationsForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  readScope,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { acceptMacRead, beginMacRead, macReadScopeKey } from "@/lib/macReadScope";
import {
  usePullRequestCommentComposer,
  usePullRequestReviewCandidates,
  type PullRequestInfo,
} from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

const posted = (login: string, body: string) => ({
  ok: true,
  data: {
    object: "session.github.pr_comment",
    comment: { author: login, body, created_at: "2026-11-05T10:00:00Z", url: `${PR}#c-${body}` },
  },
  actor: { login },
});

/** A shell reading and writing GitHub as `mac-octo`; `codeHostWrite` answers each write. */
async function installShell(
  codeHostWrite: (request: unknown) => Promise<unknown>,
  reads: readonly string[] = [],
  writes: readonly string[] = [],
) {
  const write = vi.fn(codeHostWrite);
  const cancel = vi.fn();
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
    codeHostWrite: write,
    codeHostWriteCancel: cancel,
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, ...reads],
          accounts: true,
          writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...writes] },
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  expect(
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login: "mac-octo", generation: 1 },
    }),
  ).toBe("accepted");
  setMacWritesReleaseFlagForTests(true);
  return write;
}

function infoWithComments(): PullRequestInfo {
  return {
    available: true,
    selected_pr_url: PR,
    pr: { url: PR, comments: [] },
  } as unknown as PullRequestInfo;
}

function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // The PR's info as this Mac read it, cached under its Mac scope.
  const infoKey = ["github-info", SESSION, PR, ...readScope(PR)];
  queryClient.setQueryData(infoKey, infoWithComments());
  // The same PR as the host read it, its host controls, and as another workspace user's Mac.
  const otherKeys = [
    ["github-info", SESSION],
    ["github-info", SESSION, PR],
    ["github-info", SESSION, PR, "host-mutation-context"],
    ["github-info", SESSION, PR, ...macReadScopeKey("github", "github.com", "user-2")],
  ];
  for (const otherKey of otherKeys) queryClient.setQueryData(otherKey, infoWithComments());
  const otherComments = () =>
    otherKeys.flatMap(
      (otherKey) =>
        queryClient.getQueryData<PullRequestInfo>(otherKey)?.pr?.comments?.map((c) => c.body) ?? [],
    );
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const comments = () =>
    queryClient.getQueryData<PullRequestInfo>(infoKey)?.pr?.comments?.map((c) => c.body) ?? [];
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  return { queryClient, wrapper, comments, otherComments, invalidate };
}

/** Type a comment and click post, as one gesture would. */
function postComment(
  composer: { current: ReturnType<typeof usePullRequestCommentComposer> },
  body: string,
) {
  act(() => composer.current.setBody(body));
  act(() => composer.current.post());
}

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  // A late callback from the test before may have watched the listeners just cleared.
  resetDesktopPullRequestWritesForTests();
  hostFetch.mockReset();
  toastSuccess.mockReset();
  hostFetch.mockImplementation(
    async () =>
      new Response(
        JSON.stringify({ comment: { author: "host-octo", body: "via host", url: `${PR}#host` } }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
  );
});

afterEach(() => {
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("who review candidates are read as", () => {
  const candidateReads = () =>
    (
      (window as unknown as { omnigentDesktop: { codeHostQuery: ReturnType<typeof vi.fn> } })
        .omnigentDesktop.codeHostQuery.mock.calls as [{ operation: string; account?: string }][]
    )
      .map(([request]) => request)
      .filter((request) => request.operation === "pull_request_review_candidates");

  it("stays with the host when this Mac neither writes reviewers nor lists candidates", async () => {
    await installShell(async () => posted("mac-octo", "x"));
    // Changes stay on the session's machine, so this Mac doesn't write reviewers.
    writeMacWritesIntent("off");
    const { wrapper } = setup();
    hostFetch.mockImplementation(
      async () => new Response(JSON.stringify({ object: "list", data: [], has_more: false })),
    );
    const { result } = renderHook(() => usePullRequestReviewCandidates(SESSION, PR, true), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(candidateReads()).toEqual([]);
    expect(hostFetch).toHaveBeenCalledTimes(1);
  });
});

describe("which path a comment takes", () => {
  it("stays on the host when the user chose Off, even with a complete shell and the flag on", async () => {
    const write = await installShell(async () => posted("mac-octo", "x"));
    // The user keeps every change on the session's machine.
    writeMacWritesIntent("off");
    const { wrapper } = setup();
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), { wrapper });
    postComment(result, "hello");
    await waitFor(() => expect(result.current.draft.pending).toBe(false));
    expect(hostFetch).toHaveBeenCalledTimes(1);
    expect(write).not.toHaveBeenCalled();
  });

  it("goes to this Mac as the shown account once the page implements the whole set", async () => {
    const write = await installShell(async () => posted("mac-octo", "hello"));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const { wrapper, comments, otherComments, invalidate } = setup();
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), { wrapper });
    postComment(result, "hello");
    await waitFor(() => expect(result.current.draft.pending).toBe(false));
    // GitHub's answer goes into no cache, its own scope's included: the PR is read again.
    expect(comments()).toEqual([]);
    expect(otherComments()).toEqual([]);
    expect(invalidate).toHaveBeenCalledWith({ predicate: expect.any(Function) });
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(write).toHaveBeenCalledExactlyOnceWith({
      requestId: expect.stringMatching(/^[A-Za-z0-9_-]{1,128}$/),
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_comment",
      params: { repository: "acme/app", number: 42, body: "hello" },
    });
    expect(hostFetch).not.toHaveBeenCalled();
    // A sent draft clears into the next generation, so a late upload can't land in it.
    expect(result.current.draft).toEqual({ body: "", pending: false, error: null, generation: 1 });
  });
});

describe("what a finished Mac write may touch", () => {
  async function heldComment() {
    const answer = deferred<unknown>();
    await installShell(() => answer.promise);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const harness = setup();
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), {
      wrapper: harness.wrapper,
    });
    postComment(result, "hello");
    await waitFor(() => expect(result.current.draft.pending).toBe(true));
    return { ...harness, result, answer };
  }

  it("patches nothing after the account switched, but reports and reads the PR again", async () => {
    const { result, answer, comments, invalidate } = await heldComment();
    const scope = macReadScopeKey("github", "github.com");
    expect(acceptMacRead(beginMacRead(scope), "mac-other")).toBe("transition");
    await act(async () => answer.resolve(posted("mac-octo", "hello")));
    await waitFor(() => expect(result.current.draft.pending).toBe(false));
    expect(comments()).toEqual([]);
    expect(result.current.draft.body).toBe("");
    expect(invalidatedKey(invalidate, ["github-info", SESSION, PR])).toBe(true);
    expect(toastSuccess).toHaveBeenCalledExactlyOnceWith("Commented on #42 as @mac-octo.");
  });
});

describe("a Mac write that didn't finish", () => {
  it("shows main's copy for an unknown outcome, reads the PR again, and never retries", async () => {
    const write = await installShell(async () => ({
      ok: false,
      error: {
        code: "outcome_unknown",
        message: "GitHub may have made this change. Check the pull request before trying again.",
      },
    }));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const { wrapper, invalidate } = setup();
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), { wrapper });
    postComment(result, "hello");
    await waitFor(() => expect(result.current.draft.error).not.toBeNull());
    expect(result.current.draft.error).toBe(
      "GitHub may have made this change. Check the pull request before trying again.",
    );
    expect(result.current.draft.body).toBe("hello");
    expect(invalidatedKey(invalidate, ["github-info", SESSION, PR])).toBe(true);
    expect(write).toHaveBeenCalledTimes(1);
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it("shows a definitive refusal as is, without re-reading or falling back to the host", async () => {
    await installShell(async () => ({
      ok: false,
      error: { code: "cannot_reply", message: "You can't reply to this conversation." },
    }));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const { wrapper, invalidate } = setup();
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR, "PRRT_1"), {
      wrapper,
    });
    postComment(result, "hello");
    await waitFor(() => expect(result.current.draft.error).not.toBeNull());
    expect(result.current.draft.error).toBe("You can't reply to this conversation.");
    expect(invalidate).not.toHaveBeenCalled();
    expect(hostFetch).not.toHaveBeenCalled();
  });
});

describe("an answer the shell already gave", () => {
  describe("while the browser reports being offline", () => {
    afterEach(() => onlineManager.setOnline(true));

    it("leaves the host path to wait for the network, as before", async () => {
      await installShell(async () => posted("mac-octo", "x"));
      // The user keeps every change on the session's machine.
      writeMacWritesIntent("off");
      const { wrapper } = setup();
      onlineManager.setOnline(false);
      const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), {
        wrapper,
      });
      postComment(result, "hello");
      await waitFor(() => expect(result.current.draft.pending).toBe(true));
      // React Query holds a host write until the browser is online again.
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
      expect(result.current.draft.pending).toBe(true);
      expect(hostFetch).not.toHaveBeenCalled();
      act(() => onlineManager.setOnline(true));
      await waitFor(() => expect(result.current.draft.pending).toBe(false));
      expect(hostFetch).toHaveBeenCalledOnce();
    });
  });
});

describe("what posting tells its caller", () => {
  /** Post `body` and keep the promise, counting how often it settles. */
  function postWithOutcome(
    composer: { current: ReturnType<typeof usePullRequestCommentComposer> },
    body: string,
  ) {
    act(() => composer.current.setBody(body));
    let settles = 0;
    let promise!: Promise<boolean>;
    act(() => {
      promise = composer.current.postWithOutcome().then((took) => {
        settles += 1;
        return took;
      });
    });
    return { promise, settles: () => settles };
  }

  it("says true once the host posted it, and false when the host refused", async () => {
    const { wrapper } = setup();
    const { result } = renderHook(() => usePullRequestCommentComposer(SESSION, PR), { wrapper });
    expect(await postWithOutcome(result, "hello").promise).toBe(true);
    hostFetch.mockImplementationOnce(
      async () => new Response(JSON.stringify({ detail: "No" }), { status: 400 }),
    );
    expect(await postWithOutcome(result, "again").promise).toBe(false);
  });
});

describe("closing and reopening from the comment box", () => {
  const stateAnswer = (state: string) => ({
    ok: true,
    data: { object: "session.github.pr_state", pr_url: PR, state },
    actor: { login: "mac-octo" },
  });

  /** The shell answers each operation from `answers`; `sent` lists the operations in order. */
  async function closable(answers: Record<string, () => unknown | Promise<unknown>>) {
    const sent: string[] = [];
    await installShell(
      async (request) => {
        const { operation } = request as { operation: string };
        sent.push(operation);
        return answers[operation]?.();
      },
      [],
      LIFECYCLE_WRITE_OPERATIONS,
    );
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setLifecycleFlagForTests(true);
    const harness = setup();
    const infoKey = ["github-info", SESSION, PR, ...readScope(PR)];
    harness.queryClient.setQueryData(infoKey, {
      ...infoWithComments(),
      pr: { url: PR, comments: [], state: "OPEN" },
    });
    const prState = () => harness.queryClient.getQueryData<PullRequestInfo>(infoKey)?.pr?.state;
    // What the page's own reads answer when asked again: the PR's read on this Mac, and the
    // session's list of PRs, which names the PR but carries no state of it.
    const reads = {
      scoped: async (): Promise<unknown> => harness.queryClient.getQueryData(infoKey),
      root: async (): Promise<unknown> => ({ ...infoWithComments(), pr: null }),
    };
    const rendered = renderHook(
      () => {
        useQuery({
          queryKey: infoKey,
          queryFn: () => reads.scoped(),
          staleTime: Infinity,
          retry: false,
        });
        useQuery({
          queryKey: ["github-info", SESSION],
          queryFn: () => reads.root(),
          staleTime: Infinity,
          retry: false,
        });
        const composer = usePullRequestCommentComposer(SESSION, PR);
        const action = usePullRequestStateAction({
          conversationId: SESSION,
          prUrl: PR,
          prState: "OPEN",
          post: composer.postWithOutcome,
        });
        return { composer, action };
      },
      { wrapper: harness.wrapper },
    );
    return { ...harness, ...rendered, sent, infoKey, prState, reads };
  }

  beforeEach(() => resetStateChainsForTests());

  it("posts the comment, then closes, and the PR reads as closed", async () => {
    const { result, sent, prState } = await closable({
      pull_request_comment: () => posted("mac-octo", "Closing: superseded"),
      pull_request_close: () => stateAnswer("CLOSED"),
    });
    expect(result.current.action.offer).toEqual({
      kind: "action",
      operation: "pull_request_close",
      label: "Close Pull Request",
    });
    act(() => result.current.composer.setBody("Closing: superseded"));
    act(() => result.current.action.run(true));
    expect(result.current.action.running).toBe(true);
    await waitFor(() => expect(result.current.action.running).toBe(false));
    expect(sent).toEqual(["pull_request_comment", "pull_request_close"]);
    expect(prState()).toBe("CLOSED");
    expect(result.current.action.notice).toBeNull();
  });
});
