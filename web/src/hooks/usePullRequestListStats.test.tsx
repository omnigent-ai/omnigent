import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  listStatsReaderKey,
  usePullRequestListStats,
  withKnownStats,
} from "@/hooks/usePullRequestListStats";
import {
  GITHUB_MAC_READ_OPERATIONS,
  desktopReadTarget,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { githubCooldownKey, githubCooldowns } from "@/lib/githubCooldown";
import { githubReadBudgets } from "@/lib/githubReadBudget";
import type * as IdentityModule from "@/lib/identity";
import {
  acceptMacRead,
  acceptedMacReader,
  beginMacRead,
  macReadScopeKey,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import type { PullRequestListStatsTarget } from "@/lib/pullRequestListStats";
import { PULL_REQUEST_SUMMARY_FAMILY } from "@/lib/pullRequestRecord";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const currentUser = vi.hoisted(() => ({ id: null as string | null }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
}));

const SPOKE_HOST = "2.spoke.example.com";
const SPOKE = `https://${SPOKE_HOST}/org/repo/pull/7`;
const TARGETS: PullRequestListStatsTarget[] = [
  {
    url: SPOKE,
    source: { kind: "code-host", provider: "gitspoke", host: SPOKE_HOST },
    repository: "org/repo",
    number: 7,
    headSha: undefined,
    open: true,
  },
];
const ANSWER = {
  ok: true,
  data: {
    reader: "spoke-me",
    pull_requests: [
      {
        repository: "org/repo",
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
      },
    ],
  },
};

let codeHostQuery: ReturnType<typeof vi.fn<(request: unknown) => Promise<unknown>>>;

function run(client = new QueryClient()) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return {
    client,
    ...renderHook(
      () => usePullRequestListStats(TARGETS, { enabled: true, fastTrackSettled: true }),
      {
        wrapper,
      },
    ),
  };
}

beforeEach(() => {
  currentUser.id = null;
  resetMacReadScopesForTests();
  codeHostQuery = vi.fn(async () => ANSWER);
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "gitspoke", operations: ["pull_request_list_stats"], provenance: true }],
    }),
    codeHostQuery,
  };
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  resetMacReadScopesForTests();
});

const macKeys = (client: QueryClient) =>
  client
    .getQueryCache()
    .findAll({ queryKey: ["mac-read"] })
    .map((query) => query.queryKey[1]);

describe("usePullRequestListStats on this Mac", () => {
  it.each([
    ["account_invalid", null],
    ["network", "spoke-me"],
  ])(
    "at %s from the spoke, keeps its reader only for a failure that isn't a refusal",
    async (code, kept) => {
      currentUser.id = "user-1";
      const scope = macReadScopeKey("gitspoke", SPOKE_HOST);
      acceptMacRead(beginMacRead(scope), "spoke-me");
      codeHostQuery.mockResolvedValue({ ok: false, error: { code, message: "Sign in again." } });
      const { result } = run();
      await waitFor(() => expect(result.current.gaps.get(SPOKE)).toMatchObject({ code }));
      expect(acceptedMacReader(scope)).toBe(kept);
    },
  );

  it("reads nothing without a workspace user, and reads once one is known", async () => {
    const { result, rerender, client } = run();
    // Long enough for the shell to say what it reads.
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 50);
        }),
    );
    expect(codeHostQuery).not.toHaveBeenCalled();
    expect(result.current.byUrl.size).toBe(0);
    // Not reading yet isn't a reason to show; the cell stays blank.
    expect(result.current.gaps.size).toBe(0);
    currentUser.id = "user-1";
    rerender();
    await waitFor(() => expect(result.current.byUrl.get(SPOKE)?.additions).toBe(3));
    expect(codeHostQuery).toHaveBeenCalledTimes(1);
    expect(new Set(macKeys(client))).toEqual(new Set([null, "user-1"]));
    expect(
      client.getQueryCache().find({ queryKey: ["mac-read", null], exact: false })?.state.data,
    ).toBeUndefined();
  });

  it("keeps nothing from a read whose workspace user changed while it ran", async () => {
    currentUser.id = "user-1";
    let answer: (value: unknown) => void = () => {};
    codeHostQuery.mockImplementation(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const { result, client } = run();
    await waitFor(() => expect(codeHostQuery).toHaveBeenCalledTimes(1));
    currentUser.id = "user-2";
    await act(async () => {
      answer(ANSWER);
    });
    await waitFor(() =>
      expect(
        client.getQueryCache().findAll({ queryKey: ["mac-read", "user-1"] })[0]?.state.status,
      ).toBe("error"),
    );
    expect(
      client.getQueryCache().findAll({ queryKey: ["mac-read", "user-1"] })[0]?.state.data,
    ).toBeUndefined();
    expect(result.current.byUrl.size).toBe(0);
    // A read refused for another user explains nothing on this one's rows.
    expect(result.current.gaps.get(SPOKE)).toBeUndefined();
    // Its retry isn't sent as the previous user either; the new user reads under their own key.
    expect(
      client.getQueryCache().findAll({ queryKey: ["mac-read", "user-1"] })[0]?.state.error?.message,
    ).toBe("signed_out");
    expect(macKeys(client)).toContain("user-2");
  });
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
  ...ANSWER.data.pull_requests[0],
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
    codeHostQuery,
  };
  return (window as unknown as { omnigentDesktop: { githubQuery: ReturnType<typeof vi.fn> } })
    .omnigentDesktop.githubQuery;
}

const FAST_TRACK = {
  githubFastTrack: true,
  githubFastTrackOperations: ["account_status", "pull_request_list_stats"],
  codeHosts: [{ id: "gitspoke", operations: ["pull_request_list_stats"], provenance: true }],
};

describe("usePullRequestListStats gaps", () => {
  it("explains each row from its own batch across accounts and GitSpoke", async () => {
    currentUser.id = "user-1";
    codeHostQuery.mockResolvedValue({
      ok: false,
      error: {
        code: "certificate_rejected",
        message: "GitSpoke didn't accept this Mac's certificate.",
      },
    });
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
      ...TARGETS,
    ]);
    await waitFor(() => expect(result.current.gaps.size).toBe(3));
    expect(result.current.status).toBe("ready");
    // Zero is an answer, not a gap.
    expect(result.current.byUrl.get(ALICE_ONE)).toMatchObject({ additions: 0, deletions: 0 });
    expect(result.current.gaps.has(ALICE_ONE)).toBe(false);
    // The batch answered without this row.
    expect(result.current.gaps.get(ALICE_TWO)).toEqual({ kind: "not-reported", service: "github" });
    // Bob's failure stays on Bob's row, and GitSpoke's on GitSpoke's.
    expect(result.current.gaps.get(BOB_ONE)).toEqual({
      kind: "read-failed",
      service: "github",
      code: "account_missing",
      message: "That GitHub account isn't signed in on this Mac.",
    });
    expect(result.current.gaps.get(SPOKE)).toEqual({
      kind: "read-failed",
      service: "gitspoke",
      code: "certificate_rejected",
      message: "GitSpoke didn't accept this Mac's certificate.",
    });
  });

  it("keeps no PR summary from a GitSpoke or fast-track answer, which proves no Mac reader", async () => {
    currentUser.id = "user-1";
    shell(FAST_TRACK, () => ({ ok: true, data: { pull_requests: [entryFor(1)] } }));
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(
      () =>
        usePullRequestListStats([fastTrackTarget(ALICE_ONE, "alice"), ...TARGETS], {
          enabled: true,
          fastTrackSettled: true,
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.byUrl.size).toBe(2));
    expect(client.getQueryCache().findAll({ queryKey: [PULL_REQUEST_SUMMARY_FAMILY] })).toEqual([]);
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
      { ...FAST_TRACK, githubFastTrackOperations: ["account_status"], codeHosts: [] },
      () => ({ ok: true, data: { pull_requests: [] } }),
    );
    const { result } = runTargets([fastTrackTarget(ALICE_ONE, "alice"), ...TARGETS]);
    await waitFor(() => expect(result.current.gaps.size).toBe(2));
    expect(result.current.gaps.get(ALICE_ONE)).toEqual({ kind: "outdated", service: "github" });
    // A shell offering no GitSpoke at all can't read it here, whatever its version.
    expect(result.current.gaps.get(SPOKE)).toEqual({ kind: "unavailable", service: "gitspoke" });
    expect(githubQuery).not.toHaveBeenCalled();
    expect(codeHostQuery).not.toHaveBeenCalled();
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
    currentUser.id = "user-1";
    codeHostQuery.mockResolvedValue({
      ok: false,
      error: { code: "query_failed", message: "GitSpoke couldn't answer." },
    });
    run();
    await settle();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4 * 60_000);
    });
    // Reads at 0, 60 and 180 s.
    expect(codeHostQuery).toHaveBeenCalledTimes(3);
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
  const scope = ["mac-read", "user-1", "github", "github.com"];
  const mac = (target: { provider: "github" | "gitspoke"; host: string; readAs?: string }) =>
    ({ kind: "mac", target, scope }) as const;

  it("paces a named Mac batch on the login it reads as", () => {
    const named = mac({ provider: "github", host: "GitHub.com", readAs: "bob" });
    expect(listStatsReaderKey(named)).toBe(githubCooldownKey("github.com", "bob"));
  });

  it("paces a fast-track batch on its account, and GitSpoke on no login", () => {
    expect(listStatsReaderKey(fastTrackTarget(ALICE_ONE, "alice").source)).toBe(
      githubCooldownKey("github.com", "alice"),
    );
    expect(listStatsReaderKey(TARGETS[0].source)).toBeNull();
    expect(listStatsReaderKey(mac({ provider: "gitspoke", host: SPOKE_HOST }))).toBeNull();
  });
});

describe("list stats read on this Mac", () => {
  const MAC_PR = "https://github.com/acme/app/pull/1";
  const macTarget: PullRequestListStatsTarget = {
    url: MAC_PR,
    source: {
      kind: "mac",
      target: { provider: "github", host: "github.com" },
      scope: ["mac-read", "user-1", "github", "github.com"],
    },
    repository: "acme/app",
    number: 1,
    headSha: undefined,
    open: true,
  };

  /**
   * A shell that reads list stats on this Mac as its own account, `mac-octo`; one that `proves`
   * names who read each answer.
   */
  /** `base` is the base commit the shell answers; null as an older shell, which reads none. */
  function macShell({
    proves = false,
    base = "base",
  }: { proves?: boolean; base?: string | null } = {}) {
    const query = vi.fn(async (request: { operation: string; host: string }) => {
      const identity = { host: request.host, login: "mac-octo", generation: 1 };
      const provenance = proves
        ? { provenance: { version: 1, provider: "github", host: request.host, viewer: "mac-octo" } }
        : {};
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] };
      }
      return {
        ok: true,
        data: {
          reader: "mac-octo",
          pull_requests: [{ ...entryFor(1), ...(base ? { base_sha: base } : {}) }],
        },
        identity,
        ...provenance,
      };
    });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostQuery: query,
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
            operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_list_stats"],
            accounts: true,
            provenance: true,
            rowSnapshots: 1,
            ...(proves ? { answerProvenance: 1 } : {}),
          },
        ],
      }),
    };
    return () =>
      query.mock.calls.filter(([request]) => request.operation === "pull_request_list_stats")
        .length;
  }

  beforeEach(() => {
    currentUser.id = "user-1";
    githubMacReadsPreference.write("on");
  });

  afterEach(() => {
    githubReadBudgets.clear();
    githubCooldowns.reset();
    githubMacReadsPreference.write("automatic");
    resetDesktopReadPathForTests();
    vi.useRealTimers();
  });

  const advance = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  it("keeps each row's state from its answer in the row's summary, as the reader it proved", async () => {
    macShell({ proves: true });
    // The read path learns this shell's features, as it would at launch.
    await desktopReadTarget(MAC_PR);
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    renderHook(
      () => usePullRequestListStats([macTarget], { enabled: true, fastTrackSettled: true }),
      { wrapper },
    );
    const summary = () =>
      client
        .getQueryCache()
        .findAll({ queryKey: [PULL_REQUEST_SUMMARY_FAMILY, "pr", MAC_PR] })
        .map((query) => [query.queryKey, query.state.data]);
    await waitFor(() => expect(summary()).toHaveLength(1));
    const [[key, data]] = summary();
    expect(key).toContain("mac-octo");
    expect(data).toMatchObject({
      state: { value: "OPEN" },
      isDraft: { value: false },
      // Both commits from the same answer, so the PR's patch can read before its overview.
      listedCommits: { value: { base: "base", head: "head", readAt: expect.any(Number) } },
    });
    // Stats never name a title or an author.
    expect(data).not.toHaveProperty("title");
  });

  it("keeps no listed commits from a shell that reads no base", async () => {
    macShell({ proves: true, base: null });
    await desktopReadTarget(MAC_PR);
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    renderHook(
      () => usePullRequestListStats([macTarget], { enabled: true, fastTrackSettled: true }),
      { wrapper },
    );
    const summary = () =>
      client.getQueryCache().findAll({ queryKey: [PULL_REQUEST_SUMMARY_FAMILY, "pr", MAC_PR] });
    await waitFor(() => expect(summary()).toHaveLength(1));
    expect(summary()[0].state.data).toMatchObject({ state: { value: "OPEN" } });
    expect(summary()[0].state.data).not.toHaveProperty("listedCommits");
  });

  it("polls every 30 seconds while its reader has budget", async () => {
    vi.useFakeTimers();
    const batches = macShell();
    runTargets([macTarget]);
    await advance(1_000);
    await advance(5 * 60_000);
    expect(batches()).toBe(11);
  });

  // Automatic's account is known only once a read proves it, as the first batch does here.
  it("pauses while the account it read as has spent its hourly budget", async () => {
    vi.useFakeTimers();
    const batches = macShell();
    runTargets([macTarget]);
    await advance(1_000);
    expect(batches()).toBe(1);
    // The first answer proved who Automatic reads as; that account's budget is now spent.
    githubReadBudgets.observe(githubCooldownKey("github.com", "mac-octo"), {
      resource: "graphql",
      limit: 5000,
      remaining: 100,
      resetAt: Date.now() + 30 * 60_000,
    });
    await advance(5 * 60_000);
    expect(batches()).toBeLessThanOrEqual(2);
  });

  it("waits out its reader's cooldown before polling again", async () => {
    vi.useFakeTimers();
    const batches = macShell();
    runTargets([macTarget]);
    await advance(1_000);
    githubCooldowns.rateLimited(
      githubCooldownKey("github.com", "mac-octo"),
      Date.now() + 5 * 60_000,
    );
    const before = batches();
    await advance(4 * 60_000);
    expect(batches()).toBeLessThanOrEqual(before + 1);
    await advance(3 * 60_000);
    expect(batches()).toBeGreaterThan(before + 1);
  });
});

describe("a windowed list's snapshots on this Mac", () => {
  const macRow = (number: number): PullRequestListStatsTarget => ({
    url: `https://github.com/acme/app/pull/${number}`,
    source: {
      kind: "mac",
      target: { provider: "github", host: "github.com" },
      scope: ["mac-read", "user-1", "github", "github.com"],
    },
    repository: "acme/app",
    number,
    headSha: undefined,
    open: true,
  });

  /** A Mac shell whose every batch finds one more check passed, so each answer's snapshot differs. */
  function changingMacShell() {
    const batches: number[][] = [];
    const query = vi.fn(async (request: { operation: string; host: string; params?: unknown }) => {
      const identity = { host: request.host, login: "mac-octo", generation: 1 };
      if (request.operation === "account_list") {
        return { ok: true, data: [{ login: "mac-octo", active: true, state: "success" }] };
      }
      const asked = (request.params as { pull_requests: { number: number }[] }).pull_requests;
      batches.push(asked.map((pr) => pr.number));
      const pull_requests = asked.map(({ number }) => ({
        ...entryFor(number),
        additions: number + 100 * batches.length,
        snapshot: {
          check_runs: { SUCCESS: batches.length },
          status_contexts: {},
          review_decision: null,
          review_requests: 0,
          latest_reviews: 0,
        },
      }));
      return { ok: true, data: { reader: "mac-octo", pull_requests }, identity };
    });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostQuery: query,
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
            operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_list_stats"],
            accounts: true,
            provenance: true,
            rowSnapshots: 1,
          },
        ],
      }),
    };
    return batches;
  }

  beforeEach(() => {
    currentUser.id = "user-1";
    githubMacReadsPreference.write("on");
  });

  afterEach(() => {
    githubReadBudgets.clear();
    githubCooldowns.reset();
    githubMacReadsPreference.write("automatic");
    resetDesktopReadPathForTests();
    vi.useRealTimers();
  });

  // A second at a time, each in its own act, so the hook renders as a page would between them.
  const advance = async (ms: number) => {
    for (let left = ms; left > 0; left -= 1_000) {
      // eslint-disable-next-line no-await-in-loop -- time passes in order.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(Math.min(1_000, left));
      });
    }
  };

  it("keeps a row's newest snapshot when it scrolls back into an older batch", async () => {
    vi.useFakeTimers();
    const batches = changingMacShell();
    const rows = [macRow(1), macRow(2), macRow(3)];
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const view = renderHook(
      ({ reads }: { reads: PullRequestListStatsTarget[] }) =>
        usePullRequestListStats(rows, {
          enabled: true,
          fastTrackSettled: true,
          followsWindow: true,
          readUrls: new Set(reads.map((row) => row.url)),
        }),
      { wrapper, initialProps: { reads: rows.slice(0, 2) } },
    );
    const second = rows[1]!.url;
    await advance(2_000);
    const first = view.result.current.snapshots?.get(second);
    expect(first).toBeDefined();
    view.rerender({ reads: rows.slice(1) });
    await advance(6_000);
    expect(batches).toHaveLength(2);
    const newer = view.result.current.snapshots?.get(second);
    expect(newer?.fingerprint).not.toBe(first?.fingerprint);
    // Back to the first batch while both answers are fresh: nothing reads, and the newer stands.
    view.rerender({ reads: rows.slice(0, 2) });
    await advance(2_000);
    expect(batches).toHaveLength(2);
    expect(view.result.current.snapshots?.get(second)).toEqual(newer);
    expect(view.result.current.byUrl.get(second)?.additions).toBe(202);
  });
});
