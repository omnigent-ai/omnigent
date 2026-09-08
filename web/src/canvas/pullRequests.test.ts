import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type { GithubChecks, GithubInfo } from "@/hooks/useGithub";
import * as githubHook from "@/hooks/useGithub";
import { PULL_REQUEST_CONCURRENCY, PullRequestQueue, usePullRequests } from "./pullRequests";

vi.mock("@/hooks/useGithub", () => ({ fetchGithubInfo: vi.fn() }));

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

const NO_CHECKS: GithubChecks = { passing: 0, failing: 0, pending: 0, total: 0, runs: [] };

function info(pr: GithubInfo["pr"]): GithubInfo {
  return { object: "session.github.info", available: true, pr };
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
    vi.mocked(githubHook.fetchGithubInfo).mockReset();
  });

  function wrapper({ children }: { children: ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return createElement(QueryClientProvider, { client }, children);
  }

  it("looks up only branch-bearing sessions, bounded by the concurrency limit", async () => {
    const gates = new Map<string, ReturnType<typeof deferred<GithubInfo>>>();
    vi.mocked(githubHook.fetchGithubInfo).mockImplementation((id) => {
      const gate = deferred<GithubInfo>();
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
      expect(githubHook.fetchGithubInfo).toHaveBeenCalledTimes(PULL_REQUEST_CONCURRENCY),
    );
    expect(githubHook.fetchGithubInfo).not.toHaveBeenCalledWith("plain");

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
      expect(githubHook.fetchGithubInfo).toHaveBeenCalledTimes(PULL_REQUEST_CONCURRENCY + 1),
    );

    gates.get("branch_1")!.resolve(info(null));
    await waitFor(() => expect(result.current.branch_1).toBeNull());
  });

  it("ignores pull requests without an https URL", async () => {
    vi.mocked(githubHook.fetchGithubInfo).mockResolvedValue(
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
});
