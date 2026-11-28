import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  listStatsReaderKey,
  usePullRequestListStats,
  withKnownStats,
} from "@/hooks/usePullRequestListStats";
import { githubCooldownKey, githubCooldowns } from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import type { PullRequestListStatsTarget } from "@/lib/pullRequestListStats";

const ENTRY = {
  repository: "acme/app",
  number: 7,
  additions: 3,
  deletions: 1,
  changed_files: 1,
  updated_at: null,
  head_sha: "head",
  state: "OPEN",
  is_draft: false,
  ci: null,
  ci_head_sha: "head",
};

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

const ALICE_ONE = "https://github.com/acme/app/pull/1";
const ALICE_TWO = "https://github.com/acme/app/pull/2";
const BOB_ONE = "https://github.com/other/lib/pull/9";

function fastTrackTarget(url: string, login: string): PullRequestListStatsTarget {
  const [, , , owner, repo, , number] = url.split("/");
  return {
    url,
    source: { kind: "github-fast-track", account: { host: "github.com", login } },
    repository: `${owner}/${repo}`,
    number: Number(number),
    headSha: undefined,
    open: true,
  };
}

const entryFor = (number: number) => ({
  ...ENTRY,
  repository: "acme/app",
  number,
  additions: 0,
  deletions: 0,
});

function runTargets(targets: PullRequestListStatsTarget[]) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => usePullRequestListStats(targets, { enabled: true, fastTrackSettled: true }),
    { wrapper },
  );
}

function shell(
  features: Record<string, unknown>,
  githubQuery: (operation: string, account: { login: string }, params: unknown) => unknown,
) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => features,
    githubQuery: vi.fn(async (...args: Parameters<typeof githubQuery>) => githubQuery(...args)),
  };
  return (window as unknown as { omnigentDesktop: { githubQuery: ReturnType<typeof vi.fn> } })
    .omnigentDesktop.githubQuery;
}

const FAST_TRACK = {
  githubFastTrack: true,
  githubFastTrackOperations: ["account_status", "pull_request_list_stats"],
};

describe("usePullRequestListStats gaps", () => {
  it("explains each row from its own batch across accounts", async () => {
    shell(FAST_TRACK, (_operation, account) =>
      account.login === "alice"
        ? { ok: true, data: { pull_requests: [entryFor(1), null] } }
        : {
            ok: false,
            error: {
              code: "account_missing",
              message: "That GitHub account isn't signed in on this Mac.",
            },
          },
    );
    const { result } = runTargets([
      fastTrackTarget(ALICE_ONE, "alice"),
      fastTrackTarget(ALICE_TWO, "alice"),
      fastTrackTarget(BOB_ONE, "bob"),
    ]);
    await waitFor(() => expect(result.current.gaps.size).toBe(2));
    expect(result.current.status).toBe("ready");
    // Zero is an answer, not a gap.
    expect(result.current.byUrl.get(ALICE_ONE)).toMatchObject({ additions: 0, deletions: 0 });
    expect(result.current.gaps.has(ALICE_ONE)).toBe(false);
    // The batch answered without this row.
    expect(result.current.gaps.get(ALICE_TWO)).toEqual({ kind: "not-reported", service: "github" });
    // Bob's failure stays on Bob's row.
    expect(result.current.gaps.get(BOB_ONE)).toEqual({
      kind: "read-failed",
      service: "github",
      code: "account_missing",
      message: "That GitHub account isn't signed in on this Mac.",
    });
  });

  it("keeps an unknown error's own message", async () => {
    shell(FAST_TRACK, () => ({
      ok: false,
      error: { code: "brand_new", message: "Something new." },
    }));
    const { result } = runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await waitFor(() => expect(result.current.gaps.get(ALICE_ONE)).toBeDefined());
    expect(result.current.status).toBe("failed");
    expect(result.current.gaps.get(ALICE_ONE)).toMatchObject({
      code: "brand_new",
      message: "Something new.",
    });
  });

  it("says the app is outdated without calling an operation the shell lacks", async () => {
    const githubQuery = shell(
      { ...FAST_TRACK, githubFastTrackOperations: ["account_status"] },
      () => ({ ok: true, data: { pull_requests: [] } }),
    );
    const { result } = runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await waitFor(() => expect(result.current.gaps.size).toBe(1));
    expect(result.current.gaps.get(ALICE_ONE)).toEqual({ kind: "outdated", service: "github" });
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("says the fast track is unavailable where the shell turned it off", async () => {
    const githubQuery = shell(
      { githubFastTrack: false, githubFastTrackOperations: [] },
      () => ({}),
    );
    const { result } = runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await waitFor(() =>
      expect(result.current.gaps.get(ALICE_ONE)).toEqual({
        kind: "unavailable",
        service: "github",
      }),
    );
    expect(result.current.status).toBe("unsupported");
    expect(githubQuery).not.toHaveBeenCalled();
  });
});

describe("withKnownStats", () => {
  const known = (additions: number, dispatchedAt = 0) => ({
    additions,
    deletions: 1,
    changedFiles: undefined,
    updatedAt: 1_000,
    headSha: undefined,
    dispatchedAt,
    extended: false,
  });

  it("fills rows no read answered, so the size and update-time orders work", () => {
    const merged = withKnownStats(
      {
        status: "unsupported",
        byUrl: new Map(),
        gaps: new Map([["a", { kind: "no-account" }]]),
      },
      new Map([["a", known(5)]]),
    );
    expect(merged.status).toBe("ready");
    expect(merged.byUrl.get("a")?.additions).toBe(5);
    expect(merged.gaps.has("a")).toBe(false);
  });

  it("prefers a row's own read, which is fresher", () => {
    const merged = withKnownStats(
      { status: "ready", byUrl: new Map([["a", known(9, 50)]]), gaps: new Map() },
      new Map([["a", known(5)]]),
    );
    expect(merged.byUrl.get("a")?.additions).toBe(9);
  });

  it("leaves the read alone when nothing is known", () => {
    const read = { status: "loading" as const, byUrl: new Map(), gaps: new Map() };
    expect(withKnownStats(read, new Map())).toBe(read);
  });
});

describe("usePullRequestListStats polling", () => {
  afterEach(() => {
    githubReadBudgets.clear();
    githubCooldowns.reset();
    vi.useRealTimers();
  });

  // The first read starts once the shell's features answer; later reads follow its pace.
  const settle = () =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

  const listStatsReads = (githubQuery: ReturnType<typeof vi.fn>, login: string) =>
    githubQuery.mock.calls.filter(
      ([operation, account]) =>
        operation === "pull_request_list_stats" && (account as { login: string }).login === login,
    ).length;

  it("pauses an account's list stats while its hourly budget is spent, and keeps the others at 30 s", async () => {
    vi.useFakeTimers();
    githubReadBudgets.observe(githubCooldownKey("github.com", "alice"), {
      resource: "graphql",
      limit: 5000,
      remaining: 100,
      resetAt: Date.now() + 30 * 60_000,
    });
    const githubQuery = shell(FAST_TRACK, () => ({
      ok: true,
      data: { pull_requests: [entryFor(1)] },
    }));
    runTargets([fastTrackTarget(ALICE_ONE, "alice"), fastTrackTarget(BOB_ONE, "bob")]);
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000);
    });
    expect(listStatsReads(githubQuery, "alice")).toBe(1);
    expect(listStatsReads(githubQuery, "bob")).toBe(11);
  });

  it("backs a failing read off from 60 s instead of retrying every 30 s", async () => {
    vi.useFakeTimers();
    const githubQuery = shell(FAST_TRACK, () => ({
      ok: false,
      error: { code: "query_failed", message: "GitHub couldn't answer." },
    }));
    runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4 * 60_000);
    });
    // Reads at 0, 60 and 180 s.
    expect(listStatsReads(githubQuery, "alice")).toBe(3);
  });

  it("opens a new cooldown for an older shell's bare limit after an earlier one ended, instead of retrying at once", async () => {
    vi.useFakeTimers();
    const alice = githubCooldownKey("github.com", "alice");
    // An earlier limit, long over and answered since.
    githubCooldowns.rateLimited(alice, Date.now() + 1_000);
    await vi.advanceTimersByTimeAsync(60_000);
    githubCooldowns.answered(alice);
    expect(githubCooldowns.phase(alice)).toBeNull();
    const githubQuery = shell(FAST_TRACK, () => ({
      ok: false,
      error: { code: "rate_limited", message: "GitHub is limiting requests right now." },
    }));
    runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(listStatsReads(githubQuery, "alice")).toBe(1);
    expect(githubCooldowns.phase(alice)?.phase).toBe("waiting");
    // The next read waits out the new cooldown's fallback, about a minute.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(listStatsReads(githubQuery, "alice")).toBe(1);
  });

  it("ends an account's cooldown on any other answer after the wait, so a later limit starts over at a minute", async () => {
    vi.useFakeTimers();
    const alice = githubCooldownKey("github.com", "alice");
    githubCooldowns.rateLimited(alice, Date.now() + 1_000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(githubCooldowns.phase(alice)?.phase).toBe("recovering");
    const githubQuery = shell(FAST_TRACK, () => ({
      ok: false,
      error: { code: "query_failed", message: "GitHub couldn't answer." },
    }));
    const { result } = runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await settle();
    await settle();
    expect(listStatsReads(githubQuery, "alice")).toBe(1);
    // The read still fails as itself, and the cooldown it followed is over.
    expect(result.current.gaps.get(ALICE_ONE)).toMatchObject({ code: "query_failed" });
    expect(githubCooldowns.phase(alice)).toBeNull();
    githubCooldowns.rateLimited(alice, null);
    const until = githubCooldowns.heldUntil(alice) ?? 0;
    expect(until - Date.now()).toBeLessThanOrEqual(72_000);
  });

  it("asks the shell nothing while the account's cooldown holds, as every read as it waits", async () => {
    vi.useFakeTimers();
    githubCooldowns.rateLimited(githubCooldownKey("github.com", "alice"), Date.now() + 5 * 60_000);
    const githubQuery = shell(FAST_TRACK, () => ({
      ok: true,
      data: { pull_requests: [entryFor(1)] },
    }));
    runTargets([fastTrackTarget(ALICE_ONE, "alice")]);
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(listStatsReads(githubQuery, "alice")).toBe(0);
  });
});

describe("listStatsReaderKey", () => {
  it("paces a fast-track batch on its account, and the host's on no login", () => {
    expect(listStatsReaderKey(fastTrackTarget(ALICE_ONE, "alice").source)).toBe(
      githubCooldownKey("github.com", "alice"),
    );
    expect(listStatsReaderKey({ kind: "host", sessionId: "s1", host: "github.com" })).toBeNull();
  });
});
