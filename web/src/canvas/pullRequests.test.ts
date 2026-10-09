import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type {
  PullRequestAssociation,
  PullRequestChecks,
  PullRequestInfo,
} from "@/hooks/usePullRequests";
import * as pullRequestHooks from "@/hooks/usePullRequests";
import {
  PULL_REQUEST_CONCURRENCY,
  PULL_REQUEST_REFRESH_MS,
  PULL_REQUEST_RETRY_MS,
  PullRequestQueue,
  usePullRequests,
} from "./pullRequests";

vi.mock("@/hooks/usePullRequests", () => ({ fetchPullRequestInfo: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function session(id: string, gitBranch: string | null): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: null,
    git_branch: gitBranch,
  };
}

const NO_CHECKS: PullRequestChecks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

function info(pr: PullRequestInfo["pr"], extra: Partial<PullRequestInfo> = {}): PullRequestInfo {
  return { object: "session.github.info", available: true, pr, ...extra };
}

const FORGE_URL = "https://forge.example.test/acme/proj/_git/repo/pullrequest/7";

function openPr(url: string): NonNullable<PullRequestInfo["pr"]> {
  return {
    number: 7,
    title: "Ship it",
    state: "OPEN",
    url,
    is_draft: false,
    author: null,
    base_ref: null,
    head_ref: null,
    checks: NO_CHECKS,
  };
}

function association(overrides: Partial<PullRequestAssociation> = {}): PullRequestAssociation {
  return {
    url: FORGE_URL,
    host: "forge.example.test",
    repository: "acme/proj/repo",
    number: 7,
    relationship: "created",
    ...overrides,
  };
}

describe("PullRequestQueue", () => {
  it("runs at most the configured number of tasks at once and drops replaced pending work", async () => {
    const queue = new PullRequestQueue(2);
    const gates = [deferred<void>(), deferred<void>(), deferred<void>()];
    const started: string[] = [];
    const task = (key: string, index: number) => ({
      key,
      run: () => {
        started.push(key);
        return gates[index].promise;
      },
    });
    queue.replace([task("a", 0), task("b", 1), task("c", 2)]);
    expect(started).toEqual(["a", "b"]);

    // Pending "c" is dropped; the active tasks keep running and "d" queues behind them.
    queue.replace([task("d", 2)]);
    gates[0].resolve();
    await waitFor(() => expect(started).toEqual(["a", "b", "d"]));
    gates[1].resolve();
    gates[2].resolve();
    await Promise.resolve();
    expect(started).not.toContain("c");
  });
});

describe("usePullRequests", () => {
  beforeEach(() => {
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockReset();
  });

  function wrapper({ children }: { children: ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return createElement(QueryClientProvider, { client }, children);
  }

  it("looks up only branch-bearing sessions, bounded by the concurrency limit", async () => {
    const gates = new Map<string, ReturnType<typeof deferred<PullRequestInfo>>>();
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockImplementation((id) => {
      const gate = deferred<PullRequestInfo>();
      gates.set(id, gate);
      return gate.promise;
    });
    const sessions = [
      ...Array.from({ length: PULL_REQUEST_CONCURRENCY + 2 }, (_, index) =>
        session(`branch_${index}`, `feat/${index}`),
      ),
      session("plain", null),
    ];

    const { result } = renderHook(() => usePullRequests(sessions), { wrapper });

    await waitFor(() =>
      expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(PULL_REQUEST_CONCURRENCY),
    );
    expect(pullRequestHooks.fetchPullRequestInfo).not.toHaveBeenCalledWith("plain");

    gates.get("branch_0")!.resolve(
      info({
        number: 7,
        title: "Ship it",
        state: "OPEN",
        url: "https://github.com/acme/repo/pull/7",
        is_draft: false,
        author: null,
        base_ref: null,
        head_ref: null,
        checks: NO_CHECKS,
      }),
    );
    await waitFor(() =>
      expect(result.current.branch_0).toEqual({
        number: 7,
        title: "Ship it",
        state: "OPEN",
        url: "https://github.com/acme/repo/pull/7",
      }),
    );
    // Freeing one slot starts the next queued lookup.
    await waitFor(() =>
      expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(
        PULL_REQUEST_CONCURRENCY + 1,
      ),
    );

    gates.get("branch_1")!.resolve(info(null));
    await waitFor(() => expect(result.current.branch_1).toBeNull());
  });

  it("retries a failed lookup after the retry window, not the full refresh window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(pullRequestHooks.fetchPullRequestInfo)
      .mockRejectedValueOnce(new Error("runner offline"))
      .mockResolvedValue(
        info({
          number: 3,
          title: "Back",
          state: "OPEN",
          url: "https://github.com/acme/repo/pull/3",
          is_draft: false,
          author: null,
          base_ref: null,
          head_ref: null,
          checks: NO_CHECKS,
        }),
      );
    const sessions = [session("s", "main")];
    const { result } = renderHook(() => usePullRequests(sessions), { wrapper });
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_RETRY_MS + 50);
    });
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.s).toMatchObject({ number: 3 }));

    // A successful lookup is not repeated until the full refresh window elapses.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_RETRY_MS + 50);
    });
    expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_REFRESH_MS);
    });
    await waitFor(() => expect(pullRequestHooks.fetchPullRequestInfo).toHaveBeenCalledTimes(3));
    vi.useRealTimers();
  });

  it("ignores pull requests without an https URL", async () => {
    vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockResolvedValue(
      info({
        number: 1,
        title: "Local",
        state: "OPEN",
        url: "javascript:alert(1)",
        is_draft: false,
        author: null,
        base_ref: null,
        head_ref: null,
        checks: NO_CHECKS,
      }),
    );
    const { result } = renderHook(() => usePullRequests([session("s", "main")]), { wrapper });
    await waitFor(() => expect(result.current.s).toBeNull());
  });

  interface ProviderCase {
    name: string;
    prs?: PullRequestAssociation[];
    provider?: string | null;
    provider_display?: PullRequestInfo["provider_display"];
    expected: string | null | undefined;
  }

  it.each<ProviderCase>([
    {
      name: "the PR's own provider over the session's",
      prs: [association({ provider: "example_forge" })],
      provider: "github",
      expected: "example_forge",
    },
    {
      name: "the PR's own GitHub provider over an Example Forge session",
      prs: [association({ provider: "github" })],
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      expected: "github",
    },
    {
      name: "the session's provider when the PR names none",
      prs: [association()],
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      expected: "example_forge",
    },
    {
      name: "the session's provider for an untracked PR",
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      expected: "example_forge",
    },
    { name: "no provider when the host names none", provider: null, expected: null },
    { name: "no provider from a host that predates the field", expected: undefined },
  ])(
    "carries the provider through: $name",
    async ({ prs, provider, provider_display, expected }) => {
      vi.mocked(pullRequestHooks.fetchPullRequestInfo).mockResolvedValue(
        info(openPr(FORGE_URL), { prs, provider, provider_display }),
      );
      const { result } = renderHook(() => usePullRequests([session("s", "main")]), { wrapper });
      await waitFor(() => expect(result.current.s).toMatchObject({ number: 7 }));
      expect(result.current.s?.provider).toBe(expected);
    },
  );

  it("updates the card when a refresh changes only the provider", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(pullRequestHooks.fetchPullRequestInfo)
      .mockResolvedValueOnce(info(openPr(FORGE_URL), { provider: "github" }))
      .mockResolvedValue(info(openPr(FORGE_URL), { provider: "example_forge" }));
    const { result } = renderHook(() => usePullRequests([session("s", "main")]), { wrapper });
    await waitFor(() => expect(result.current.s?.provider).toBe("github"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_REFRESH_MS + PULL_REQUEST_RETRY_MS + 50);
    });
    await waitFor(() => expect(result.current.s?.provider).toBe("example_forge"));
    vi.useRealTimers();
  });
});
