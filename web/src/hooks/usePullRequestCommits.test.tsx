import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { checkGitHubFastTrackAccount, queryGitHubFastTrack } from "@/lib/githubFastTrack";
import type * as GitHubFastTrackModule from "@/lib/githubFastTrack";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import type { GitHubFastTrackAccount } from "@/lib/nativeBridge";
import { COMMITS_SLOW_AFTER_MS, type CommitsReadPath } from "@/lib/pullRequestCommits";

import { gitHubFastTrackStatusKey, useGitHubFastTrack } from "./useGitHubFastTrack";
import {
  commitDiffQueryKey,
  type PullRequestCommitsSource,
  restartQuery,
  useCommitListReconciliation,
  commitDiffShownFor,
  COMMIT_READ_AHEAD_IN_FLIGHT,
  useCommitDiff,
  useCommitDiffReadAhead,
  useCommitsTabStatus,
  usePullRequestCommits,
} from "./usePullRequestCommits";

vi.mock("@/lib/githubFastTrack", async (importOriginal) => ({
  ...(await importOriginal<typeof GitHubFastTrackModule>()),
  queryGitHubFastTrack: vi.fn(),
  checkGitHubFastTrackAccount: vi.fn(),
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  hasGitHubFastTrackBridge: () => true,
}));

const ACCOUNT: GitHubFastTrackAccount = { host: "github.com", login: "dev" };
const OPERATIONS = ["pull_request_commits", "commit_diff"] as const;
const source = (headSha = "a".repeat(40)): PullRequestCommitsSource => ({
  account: ACCOUNT,
  repository: "acme/app",
  number: 7,
  headSha,
  baseSha: "e".repeat(40),
});
const listOf = (subject: string) => ({
  ok: true as const,
  data: {
    total_count: 1,
    commits: [
      {
        sha: "c".repeat(40),
        subject,
        author_login: null,
        author_name: null,
        authored_at: null,
        committed_at: null,
        url: null,
        ci_state: null,
      },
    ],
  },
});

/** A read the test answers when it chooses. */
function held<T>() {
  let answer!: (value: T) => void;
  const promise = new Promise<T>((resolve) => {
    answer = resolve;
  });
  return { promise, answer };
}

describe("restarting a commit read", () => {
  let queryClient: QueryClient;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );

  beforeEach(() => {
    queryClient = new QueryClient();
    vi.mocked(queryGitHubFastTrack).mockReset();
    vi.mocked(checkGitHubFastTrackAccount).mockReset();
  });
  afterEach(() => vi.useRealTimers());

  /** The tab's list and status, as the panel wires them. */
  function useTab({
    path,
    listSource,
    account,
  }: {
    path: CommitsReadPath;
    listSource: PullRequestCommitsSource | null;
    account: GitHubFastTrackAccount | null;
  }) {
    const fastTrack = useGitHubFastTrack(account, OPERATIONS);
    const commits = usePullRequestCommits(listSource);
    const tab = useCommitsTabStatus({
      path,
      fastTrack,
      fastTrackKey: gitHubFastTrackStatusKey(account, OPERATIONS),
      source: listSource,
      commits,
      onTab: true,
    });
    return { ...tab, commits };
  }

  it("restarts a hanging first list read, and drops its late answer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const first = held<ReturnType<typeof listOf>>();
    vi.mocked(queryGitHubFastTrack)
      .mockReturnValueOnce(first.promise as never)
      .mockResolvedValueOnce(listOf("second read") as never);
    const { result } = renderHook(
      () => useTab({ path: "host", listSource: source(), account: null }),
      { wrapper },
    );
    expect(result.current.status).toEqual({ kind: "loading" });
    act(() => vi.advanceTimersByTime(COMMITS_SLOW_AFTER_MS));
    expect(result.current.status).toEqual({ kind: "slow" });

    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toEqual({ kind: "ready" }));
    expect(queryGitHubFastTrack).toHaveBeenCalledTimes(2);
    expect(result.current.commits.data?.commits[0]!.subject).toBe("second read");

    await act(async () => first.answer(listOf("late first read")));
    expect(result.current.commits.data?.commits[0]!.subject).toBe("second read");
  });

  it("restarts a hanging first account check, then reads the list", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const firstCheck = held<{ status: "ready"; account: GitHubFastTrackAccount }>();
    vi.mocked(checkGitHubFastTrackAccount)
      .mockReturnValueOnce(firstCheck.promise)
      .mockResolvedValueOnce({ status: "ready", account: ACCOUNT });
    vi.mocked(queryGitHubFastTrack).mockResolvedValue(listOf("listed") as never);
    const { result, rerender } = renderHook(
      ({ listSource }: { listSource: PullRequestCommitsSource | null }) =>
        useTab({ path: "fast-track", listSource, account: ACCOUNT }),
      { wrapper, initialProps: { listSource: null as PullRequestCommitsSource | null } },
    );
    expect(result.current.status).toEqual({ kind: "checking" });
    act(() => vi.advanceTimersByTime(COMMITS_SLOW_AFTER_MS));
    expect(result.current.status).toEqual({ kind: "slow" });

    act(() => result.current.retry());
    await waitFor(() => expect(checkGitHubFastTrackAccount).toHaveBeenCalledTimes(2));
    // The panel builds the list's source once the check is ready.
    rerender({ listSource: source() });
    await waitFor(() => expect(result.current.status).toEqual({ kind: "ready" }));
    await act(async () => firstCheck.answer({ status: "ready", account: ACCOUNT }));
    expect(checkGitHubFastTrackAccount).toHaveBeenCalledTimes(2);
  });

  it("restarts a hanging first diff read, and drops its late answer", async () => {
    const first = held<{ ok: true; data: { sha: string; patch: string } }>();
    vi.mocked(queryGitHubFastTrack)
      .mockReturnValueOnce(first.promise as never)
      .mockResolvedValueOnce({ ok: true, data: { sha: "c", patch: "SECOND" } } as never);
    const sha = "c".repeat(40);
    const { result } = renderHook(() => useCommitDiff(source(), sha), { wrapper });
    expect(result.current.isPending).toBe(true);

    await act(() => restartQuery(queryClient, commitDiffQueryKey(source(), sha)));
    await waitFor(() => expect(result.current.data?.patch).toBe("SECOND"));
    expect(queryGitHubFastTrack).toHaveBeenCalledTimes(2);
    await act(async () => first.answer({ ok: true, data: { sha: "c", patch: "LATE" } }));
    expect(result.current.data?.patch).toBe("SECOND");
  });

  it("gives a new pending request its own time bound", () => {
    vi.useFakeTimers();
    vi.mocked(queryGitHubFastTrack).mockReturnValue(new Promise(() => {}) as never);
    const { result, rerender } = renderHook(
      ({ head }: { head: string }) =>
        useTab({ path: "host", listSource: source(head), account: null }),
      { wrapper, initialProps: { head: "a".repeat(40) } },
    );
    act(() => vi.advanceTimersByTime(COMMITS_SLOW_AFTER_MS - 1_000));
    // A push moves the head while the first read still hangs.
    rerender({ head: "b".repeat(40) });
    act(() => vi.advanceTimersByTime(2_000));
    expect(result.current.status).toEqual({ kind: "loading" });
    act(() => vi.advanceTimersByTime(COMMITS_SLOW_AFTER_MS));
    expect(result.current.status).toEqual({ kind: "slow" });
  });

  it("never times out once the list has loaded", async () => {
    vi.mocked(queryGitHubFastTrack).mockResolvedValue(listOf("listed") as never);
    const { result } = renderHook(
      () => useTab({ path: "host", listSource: source(), account: null }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toEqual({ kind: "ready" }));
    vi.useFakeTimers();
    act(() => vi.advanceTimersByTime(COMMITS_SLOW_AFTER_MS * 2));
    expect(result.current.status).toEqual({ kind: "ready" });
  });
});

describe("useCommitListReconciliation", () => {
  const listAt = (head: string, base: string) => ({
    total_count: 0,
    commits: [],
    head_sha: head,
    base_sha: base,
    complete: true,
  });
  const H1 = "1".repeat(40);
  const H2 = "2".repeat(40);
  const B = "b".repeat(40);

  it("re-reads the pull request once per answered pair, so disagreeing answers can't loop", () => {
    const reread = vi.fn();
    const { rerender } = renderHook(
      ({ list }: { list: ReturnType<typeof listAt> | undefined }) =>
        useCommitListReconciliation(list, { headSha: H1, baseSha: B }, reread),
      { initialProps: { list: listAt(H1, B) as ReturnType<typeof listAt> | undefined } },
    );
    expect(reread).not.toHaveBeenCalled();
    rerender({ list: listAt(H2, B) });
    expect(reread).toHaveBeenCalledOnce();
    // The answers keep alternating; each pair re-reads only the first time.
    rerender({ list: listAt(H1, B) });
    rerender({ list: listAt(H2, B) });
    rerender({ list: listAt(H1, "c".repeat(40)) });
    expect(reread).toHaveBeenCalledTimes(2);
  });

  it("never re-reads for an older app's list that doesn't name its revision", () => {
    const reread = vi.fn();
    renderHook(() =>
      useCommitListReconciliation(
        { total_count: 0, commits: [] },
        { headSha: H1, baseSha: B },
        reread,
      ),
    );
    expect(reread).not.toHaveBeenCalled();
  });
});

/** The PR details a commit diff shows in. */
const DETAILS = { id: Symbol("details") };

describe("a commit diff on screen", () => {
  it("counts as shown only for its pull request, in the details showing it, while it shows", async () => {
    const { PullRequestRefreshOwnerContext } =
      await import("@/pullRequestPage/pullRequestRefreshOwner");
    vi.mocked(queryGitHubFastTrack).mockReturnValue(new Promise(() => {}) as never);
    const queryClient = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <PullRequestRefreshOwnerContext.Provider value={DETAILS}>
          {children}
        </PullRequestRefreshOwnerContext.Provider>
      </QueryClientProvider>
    );
    const sha = "c".repeat(40);
    const { unmount } = renderHook(() => useCommitDiff(source(), sha), { wrapper });
    const key = commitDiffQueryKey(source(), sha);
    const cache = queryClient.getQueryCache();
    const thisPr = { host: "github.com", repository: "ACME/app", number: 7 };
    expect(commitDiffShownFor(cache, key, thisPr, DETAILS)).toBe(true);
    expect(commitDiffShownFor(cache, key, { ...thisPr, number: 8 }, DETAILS)).toBe(false);
    expect(commitDiffShownFor(cache, key, thisPr, { id: Symbol("other details") })).toBe(false);
    expect(commitDiffShownFor(cache, key, thisPr, null)).toBe(false);
    expect(commitDiffShownFor(new QueryClient().getQueryCache(), key, thisPr, DETAILS)).toBe(false);
    unmount();
    expect(commitDiffShownFor(cache, key, thisPr, DETAILS)).toBe(false);
  });
});

describe("reading commits ahead of a step", () => {
  const sha = (index: number) => index.toString(16).padStart(40, "0");
  let queryClient: QueryClient;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  /** Every fast-track read so far, each answered when the test chooses. */
  let reads: { sha: string; answer: () => void; answered: boolean }[];
  const readShas = () => reads.map((read) => read.sha);
  const settle = () => act(async () => {});

  beforeEach(() => {
    queryClient = new QueryClient();
    reads = [];
    vi.mocked(queryGitHubFastTrack).mockReset();
    vi.mocked(queryGitHubFastTrack).mockImplementation(
      (_operation, _account, params) =>
        new Promise((resolve) => {
          const read = {
            sha: (params as { sha: string }).sha,
            answered: false,
            answer: () => {
              read.answered = true;
              resolve({ ok: true, data: { sha: read.sha, patch: "PATCH" } } as never);
            },
          };
          reads.push(read);
        }),
    );
  });

  /** The hook over a window the test moves, as the tab moves it on each step. */
  function renderReadAhead(
    initial: readonly string[],
    from: PullRequestCommitsSource | null = source(),
  ) {
    return renderHook(
      ({
        shas,
        readSource,
      }: {
        shas: readonly string[];
        readSource: PullRequestCommitsSource | null;
      }) => useCommitDiffReadAhead(readSource, shas),
      { wrapper, initialProps: { shas: initial, readSource: from } },
    );
  }

  it("reads the window's commits a few at a time, into the open view's cache", async () => {
    renderReadAhead([sha(2), sha(0), sha(3)]);
    await settle();
    expect(readShas()).toEqual([sha(2), sha(0)].slice(0, COMMIT_READ_AHEAD_IN_FLIGHT));
    await act(async () => reads[0]!.answer());
    // A slot that answers lets the next one go.
    expect(readShas()).toEqual([sha(2), sha(0), sha(3)]);

    const { result } = renderHook(() => useCommitDiff(source(), sha(2)), { wrapper });
    await waitFor(() => expect(result.current.data?.patch).toBe("PATCH"));
    expect(reads).toHaveLength(3);
  });

  it("doesn't read a commit it already has again", async () => {
    queryClient.setQueryData(commitDiffQueryKey(source(), sha(2)), {
      sha: sha(2),
      patch: "CACHED",
    });
    renderReadAhead([sha(2), sha(0)]);
    await settle();
    expect(readShas()).toEqual([sha(0)]);
  });

  it("lets the open commit join the read ahead under way, and lets go of nothing running", async () => {
    const view = renderReadAhead([sha(2), sha(0)]);
    await settle();
    expect(readShas()).toEqual([sha(2), sha(0)]);
    const { result } = renderHook(() => useCommitDiff(source(), sha(2)), { wrapper });
    await settle();
    expect(reads).toHaveLength(2);
    // Stepping on lets go of nothing under way: cancelling would only lose its answer.
    view.rerender({ shas: [sha(3)], readSource: source() });
    await settle();
    expect(readShas()).toEqual([sha(2), sha(0)]);
    await act(async () => reads[0]!.answer());
    await waitFor(() => expect(result.current.data?.patch).toBe("PATCH"));
    await act(async () => reads[1]!.answer());
    expect(readShas()).toEqual([sha(2), sha(0), sha(3)]);
  });

  it("keeps a failed read ahead quiet, and the step reads it again", async () => {
    vi.mocked(queryGitHubFastTrack).mockRejectedValueOnce(new Error("offline"));
    renderReadAhead([sha(2)]);
    await settle();
    const { result } = renderHook(() => useCommitDiff(source(), sha(2)), { wrapper });
    await settle();
    await act(async () => reads.at(-1)!.answer());
    await waitFor(() => expect(result.current.data?.patch).toBe("PATCH"));
  });

  it("reads nothing while there's no source", async () => {
    renderReadAhead([sha(2)], null);
    await settle();
    expect(queryGitHubFastTrack).not.toHaveBeenCalled();
  });
});
