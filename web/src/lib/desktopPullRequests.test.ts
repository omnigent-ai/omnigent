import type * as IdentityModule from "@/lib/identity";
import { afterEach, describe, expect, it, vi } from "vitest";

const { currentUser } = vi.hoisted(() => ({ currentUser: { id: "user-1" as string | null } }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
}));

import {
  fetchPullRequestFileContents,
  fetchPullRequestInfo,
  withSessionAssociations,
  type PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";
import {
  DesktopPullRequestError,
  desktopPullRequestTarget,
  fetchDesktopPullRequestChangedFiles,
  fetchDesktopPullRequestCommitDiff,
  fetchDesktopPullRequestCommits,
  fetchDesktopPullRequestDiff,
  fetchDesktopPullRequestInfo,
} from "./desktopPullRequests";
import { autoMergeKnown } from "./pullRequestAutoMerge";
import { GITHUB_MAC_READ_OPERATIONS, githubMacReadsPreference, readScope } from "./desktopReadPath";
import {
  acceptMacRead,
  beginMacRead,
  macReadScopeKey,
  onMacReadScopeAdvanced,
} from "./macReadScope";
import { GITSPOKE_READ_OPERATIONS } from "./localPullRequestLinks";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

// The session and PR from the GitSpoke acceptance check.
const SESSION = "3869946998045662";
const SPOKE_PR = "https://2.spoke.example.com/example-org/universe/pull/20002346";
const HEAD = "a".repeat(40);
const BASE = "b".repeat(40);

const OVERVIEW = {
  repository: "example-org/universe",
  pr: {
    number: 20002346,
    title: "Fix the thing",
    state: "OPEN",
    url: SPOKE_PR,
    is_draft: false,
    author: "author",
    base_ref: "master",
    head_ref: "feature",
    head_sha: HEAD,
    base_sha: BASE,
    body: null,
    checks: { passing: 1, failing: 0, pending: 0, total: 1, runs: [] },
    checks_supported: true,
  },
};

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

function capableShell(
  answers: Record<string, unknown> = {},
  operations: readonly string[] = GITSPOKE_READ_OPERATIONS,
) {
  const codeHostQuery = vi.fn(async (request: { operation: string }) => {
    const answer = answers[request.operation];
    return answer && typeof answer === "object" && "ok" in answer
      ? answer
      : { ok: true, data: answer };
  });
  installShell({
    codeHostQuery,
    localPullRequestLinks: async () => ({
      ok: true,
      data: [
        {
          url: SPOKE_PR,
          provider: "gitspoke",
          host: "2.spoke.example.com",
          repository: "example-org/universe",
          number: 20002346,
          linked_at: 1,
        },
      ],
    }),
    getDesktopFeatures: async () => ({
      localPullRequestLinks: true,
      codeHosts: [{ id: "gitspoke", operations: [...operations] }],
    }),
  });
  return codeHostQuery;
}

afterEach(() => {
  installShell(undefined);
  vi.unstubAllGlobals();
});

describe("desktopPullRequestTarget", () => {
  it("reads a GitSpoke PR on any shard on a capable shell, and nothing else", async () => {
    capableShell();
    for (const shard of ["1", "2", "40"]) {
      const url = SPOKE_PR.replace("//2.", `//${shard}.`);
      // eslint-disable-next-line no-await-in-loop -- one shard at a time names the failing one.
      expect(await desktopPullRequestTarget(url)).toMatchObject({
        provider: "gitspoke",
        number: 20002346,
      });
    }
    expect(await desktopPullRequestTarget("https://github.com/o/r/pull/1")).toBeNull();
    expect(await desktopPullRequestTarget(undefined)).toBeNull();
  });

  it("leaves GitSpoke PRs to the host on shells without every read", async () => {
    installShell({
      codeHostQuery: vi.fn(),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: ["pull_request_lookup"] }],
      }),
    });
    expect(await desktopPullRequestTarget(SPOKE_PR)).toBeNull();
    installShell(undefined);
    expect(await desktopPullRequestTarget(SPOKE_PR)).toBeNull();
  });
});

describe("the desktop data source", () => {
  it("shapes the overview as the panel's info for the selected PR", async () => {
    capableShell({ pull_request_overview: OVERVIEW });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    const associations = [
      {
        url: SPOKE_PR,
        host: target.host,
        repository: target.repository,
        number: 20002346,
        relationship: "attached" as const,
      },
    ];
    const info = await fetchDesktopPullRequestInfo(target, Promise.resolve(associations));
    expect(info).toMatchObject({
      available: true,
      provider: "gitspoke",
      selected_pr_url: SPOKE_PR,
      auth: { authenticated: true },
      capabilities: { account_switching: false },
      repo: { name_with_owner: "example-org/universe" },
      base_ref: "master",
      pr: { number: 20002346, title: "Fix the thing", head_sha: HEAD },
    });
    // The list row shows the title and state the overview read.
    expect(info.prs?.[0]).toMatchObject({ title: "Fix the thing", state: "OPEN" });
    // Nothing the panel offers to edit or merge through the host.
    expect(info.capabilities).not.toHaveProperty("merging");
  });

  it("keeps what the overview says of auto-merge, and nothing where an older shell's leaves it out", async () => {
    const autoMergeOf = async (pr: Record<string, unknown>) => {
      capableShell({ pull_request_overview: { ...OVERVIEW, pr: { ...OVERVIEW.pr, ...pr } } });
      const target = (await desktopPullRequestTarget(SPOKE_PR))!;
      return autoMergeKnown((await fetchDesktopPullRequestInfo(target, Promise.resolve([]))).pr);
    };
    const on = { merge_method: "SQUASH", enabled_by: "amy", enabled_at: "2026-11-05T10:00:00Z" };
    expect(await autoMergeOf({ auto_merge: on })).toBe("on");
    expect(await autoMergeOf({ auto_merge: null })).toBe("off");
    expect(await autoMergeOf({})).toBe("unknown");
  });

  it("keeps the source repository the overview names, null when unknown, absent from older shells", async () => {
    const sourceOf = async (pr: Record<string, unknown>) => {
      capableShell({ pull_request_overview: { ...OVERVIEW, pr: { ...OVERVIEW.pr, ...pr } } });
      const target = (await desktopPullRequestTarget(SPOKE_PR))!;
      return (await fetchDesktopPullRequestInfo(target, Promise.resolve([]))).pr!;
    };
    const fork = "https://2.spoke.example.com/contributor/universe";
    expect((await sourceOf({ head_repository_url: fork })).head_repository_url).toBe(fork);
    // A new shell that asked and found none: the file links say it isn't known.
    const unknown = await sourceOf({ head_repository_url: null });
    expect(unknown).toHaveProperty("head_repository_url", null);
    // An older shell never asked: the file links fall back to the PR's repository.
    expect(await sourceOf({})).not.toHaveProperty("head_repository_url");
  });

  it("passes the shell's refusal and message through", async () => {
    const refusal = {
      ok: false,
      error: { code: "certificate_rejected", message: "Renew your certificate." },
    };
    capableShell({ pull_request_overview: refusal });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    const error = await fetchDesktopPullRequestInfo(target, Promise.resolve([])).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DesktopPullRequestError);
    expect(error).toMatchObject({
      code: "certificate_rejected",
      message: "Renew your certificate.",
    });
  });

  it("reads files, and the patch at the commits the panel shows", async () => {
    const query = capableShell({
      pull_request_files: { data: [{ path: "a.txt", status: "modified" }], has_more: true },
      pull_request_patch: { patch: "diff --git a/a.txt b/a.txt\n", base_sha: BASE, head_sha: HEAD },
    });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    expect(await fetchDesktopPullRequestChangedFiles(target)).toEqual({
      available: true,
      data: [{ path: "a.txt", status: "modified" }],
      has_more: true,
    });
    expect(await fetchDesktopPullRequestDiff(target, `${BASE}:${HEAD}`)).toEqual({
      object: "session.github.pr_diff",
      patch: "diff --git a/a.txt b/a.txt\n",
      base_sha: BASE,
      head_sha: HEAD,
    });
    // The shell compares those commits, and may answer a patch it already read for them.
    expect(query).toHaveBeenLastCalledWith(
      expect.objectContaining({
        operation: "pull_request_patch",
        params: {
          repository: "example-org/universe",
          number: 20002346,
          base_sha: BASE,
          head_sha: HEAD,
          patch_cache: 1,
          fresh: false,
        },
      }),
    );
    // A Refresh asks the shell to read GitHub again.
    await fetchDesktopPullRequestDiff(target, `${BASE}:${HEAD}`, { fresh: true });
    expect(query).toHaveBeenLastCalledWith(
      expect.objectContaining({
        operation: "pull_request_patch",
        params: expect.objectContaining({ patch_cache: 1, fresh: true }),
      }),
    );
  });

  it("stamps a patch with the commits the shell compared, not the ones asked for", async () => {
    const moved = "c".repeat(40);
    capableShell({
      pull_request_patch: {
        patch: "diff --git a/b.txt b/b.txt\n",
        base_sha: BASE,
        head_sha: moved,
      },
    });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    expect(await fetchDesktopPullRequestDiff(target)).toMatchObject({
      base_sha: BASE,
      head_sha: moved,
    });
  });

  it("passes the shell's refusal when the PR moved past the commits shown", async () => {
    capableShell({
      pull_request_patch: {
        ok: false,
        error: {
          code: "pull_request_changed",
          message: "The pull request changed. Refresh, then try again.",
        },
      },
    });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    await expect(fetchDesktopPullRequestDiff(target, `${BASE}:${HEAD}`)).rejects.toMatchObject({
      code: "pull_request_changed",
    });
  });
});

const GITHUB_PR = {
  url: "https://github.com/o/r/pull/1",
  host: "github.com",
  repository: "o/r",
  number: 1,
  relationship: "created",
};

function hostInfoResponse() {
  return new Response(
    JSON.stringify({
      object: "session.github.info",
      available: true,
      tracking_available: false,
      prs: [GITHUB_PR],
    }),
    { status: 200 },
  );
}

const runnerOffline = () =>
  new Response(JSON.stringify({ error: { code: "runner_unavailable" } }), { status: 503 });

describe("routing the panel's reads", () => {
  it("reads a selected GitSpoke PR on this Mac, never asking the session's host", async () => {
    const fetch = vi.fn(async () => hostInfoResponse());
    vi.stubGlobal("fetch", fetch);
    const query = capableShell({ pull_request_overview: OVERVIEW });
    for (const options of [undefined, { hostServeable: false }]) {
      // eslint-disable-next-line no-await-in-loop -- each case names its own failure.
      const info = await fetchPullRequestInfo(SESSION, SPOKE_PR, options);
      expect(info).toMatchObject({ provider: "gitspoke", pr: { number: 20002346 } });
      // The links kept on this Mac; the hook adds the host's from the session's own info.
      expect(info.prs?.map((pr) => pr.url)).toEqual([SPOKE_PR]);
    }
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({ operation: "pull_request_overview" }),
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("lists the session's GitSpoke links alongside the host's own", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => hostInfoResponse()),
    );
    capableShell();
    const info = await fetchPullRequestInfo(SESSION);
    expect(info.tracking_available).toBe(true);
    expect(info.prs?.map((pr) => [pr.url, pr.saved_on_device ?? false])).toEqual([
      ["https://github.com/o/r/pull/1", false],
      [SPOKE_PR, true],
    ]);
  });

  it("still lists this Mac's links when the session's host is offline", async () => {
    const fetch = vi.fn(async () => runnerOffline());
    vi.stubGlobal("fetch", fetch);
    capableShell();
    for (const options of [undefined, { hostServeable: false }]) {
      // eslint-disable-next-line no-await-in-loop -- each case names its own failure.
      const info = await fetchPullRequestInfo(SESSION, undefined, options);
      expect(info).toMatchObject({
        available: true,
        tracking_available: true,
        selected_pr_url: SPOKE_PR,
      });
      expect(info.prs?.map((pr) => pr.url)).toEqual([SPOKE_PR]);
    }
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("reports the host offline when this Mac has no links to show instead", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => runnerOffline()),
    );
    installShell(undefined);
    await expect(fetchPullRequestInfo(SESSION)).rejects.toBeInstanceOf(RunnerOfflineError);
    await expect(
      fetchPullRequestInfo(SESSION, undefined, { hostServeable: false }),
    ).rejects.toBeInstanceOf(RunnerOfflineError);
  });

  it("expands a GitSpoke file's context on this Mac at the commits the panel shows", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const query = capableShell({
      pull_request_file_contents: { path: "new.txt", before: "old", after: "new" },
    });
    const contents = await fetchPullRequestFileContents(SESSION, "new.txt", "master", {
      pr_url: SPOKE_PR,
      previous_path: "old.txt",
      head_sha: HEAD,
      base_sha: BASE,
    });
    expect(contents).toEqual({
      object: "session.github.file_diff",
      path: "new.txt",
      before: "old",
      after: "new",
    });
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({
        operation: "pull_request_file_contents",
        params: {
          repository: "example-org/universe",
          number: 20002346,
          path: "new.txt",
          previous_path: "old.txt",
          head_sha: HEAD,
          base_sha: BASE,
        },
      }),
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps a GitSpoke PR on the host path in a browser", async () => {
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ object: "session.github.info", available: true }), {
          status: 200,
        }),
    );
    vi.stubGlobal("fetch", fetch);
    await fetchPullRequestInfo(SESSION, SPOKE_PR);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String((fetch.mock.calls[0] as unknown[])[0])).toContain("pr_url=");
  });
});

describe("withSessionAssociations", () => {
  const spoke = {
    url: SPOKE_PR,
    host: "2.spoke.example.com",
    repository: "example-org/universe",
    number: 20002346,
    relationship: "attached" as const,
  };
  const selected: { data?: PullRequestInfo } = {
    data: {
      object: "session.github.info",
      available: true,
      prs: [{ ...spoke, title: "Fresh title", saved_on_device: true }],
    },
  };

  it("lists the session's PRs first, keeping the selected PR's fresher entry", () => {
    const merged = withSessionAssociations(selected, {
      object: "session.github.info",
      available: true,
      prs: [GITHUB_PR as never, { ...spoke, title: "Stale title" }],
    });
    expect(merged.data?.tracking_available).toBe(true);
    expect(merged.data?.prs?.map((pr) => [pr.url, pr.title ?? null])).toEqual([
      [GITHUB_PR.url, null],
      [SPOKE_PR, "Fresh title"],
    ]);
  });

  it("tracks an emptied session list, so the session's stored and sub-agent PRs still list", () => {
    // A PR this Mac reads, not linked on this Mac, while the session's own links were all removed.
    const notTracked: { data?: PullRequestInfo } = {
      data: { object: "session.github.info", available: true, tracking_available: false, prs: [] },
    };
    const merged = withSessionAssociations(notTracked, {
      object: "session.github.info",
      available: true,
      tracking_available: true,
      prs: [],
    });
    expect(merged.data?.tracking_available).toBe(true);
    expect(merged.data?.prs).toEqual([]);
  });

  it("leaves the PR's own read alone where the session tracks nothing and lists nothing", () => {
    const untracked: PullRequestInfo = { object: "session.github.info", available: true, prs: [] };
    expect(withSessionAssociations(selected, untracked)).toBe(selected);
  });

  it("shows one shared record as each session's own: its links, tracking and provider name", () => {
    // The PR's shared record, read once for the page and every session that shows it.
    const record: { data?: PullRequestInfo } = {
      data: {
        object: "session.github.info",
        available: true,
        selected_pr_url: SPOKE_PR,
        tracking_available: false,
        provider_display: null,
        prs: [],
      },
    };
    const display = { name: "GitSpoke" } as never;
    const sessionA: PullRequestInfo = {
      object: "session.github.info",
      available: true,
      tracking_available: true,
      prs: [{ ...spoke, provider_display: display }, GITHUB_PR as never],
    };
    const sessionB: PullRequestInfo = {
      object: "session.github.info",
      available: true,
      tracking_available: true,
      prs: [{ ...spoke, relationship: "worked_on" }],
    };
    // The page shows the record as it is; each session lays its own list over it.
    expect(withSessionAssociations(record, undefined)).toBe(record);
    const inA = withSessionAssociations(record, sessionA).data!;
    const inB = withSessionAssociations(record, sessionB).data!;
    expect(inA.prs?.map((pr) => pr.url)).toEqual([SPOKE_PR, GITHUB_PR.url]);
    expect(inA.provider_display).toBe(display);
    expect(inB.prs?.map((pr) => [pr.url, pr.relationship])).toEqual([[SPOKE_PR, "worked_on"]]);
    expect(inB.provider_display).toBeNull();
    // Neither session's view changes the record the others show.
    expect(record.data?.prs).toEqual([]);
    expect(record.data?.tracking_available).toBe(false);
  });

  it("leaves the PR's own read alone until the session's info is known", () => {
    expect(withSessionAssociations(selected, undefined)).toBe(selected);
    expect(withSessionAssociations({ data: undefined }, selected.data)).toEqual({
      data: undefined,
    });
  });
});

describe("a GitHub PR read as this Mac's own account", () => {
  const MAC_GITHUB_PR = "https://github.com/example-org/universe/pull/42";
  const GITHUB_OVERVIEW = {
    repository: "example-org/universe",
    pr: { ...OVERVIEW.pr, number: 42, url: MAC_GITHUB_PR },
  };
  const identity = (login: string, generation: number) => ({
    host: "github.com",
    login,
    generation,
  });

  function githubShell(answer: (request: Record<string, unknown>) => unknown) {
    const codeHostQuery = vi.fn(async (request: Record<string, unknown>) => answer(request));
    installShell({
      codeHostQuery,
      codeHostAccounts: {
        get: async () => ({
          ok: true,
          data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
        }),
        set: async () => ({ ok: false }),
        onChanged: () => () => {},
      },
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true }],
      }),
    });
    githubMacReadsPreference.write("on");
    return codeHostQuery;
  }

  afterEach(() => githubMacReadsPreference.write("automatic"));

  it("asks for the Mac's account, never the host's login, and says who read it", async () => {
    const query = githubShell(() => ({
      ok: true,
      data: GITHUB_OVERVIEW,
      identity: identity("octo", 1),
    }));
    const target = (await desktopPullRequestTarget(MAC_GITHUB_PR))!;
    const info = await fetchDesktopPullRequestInfo(target, Promise.resolve([]));
    expect(query).toHaveBeenCalledWith(
      expect.objectContaining({ provider: "github", host: "github.com", account: "mac" }),
    );
    expect(query.mock.calls[0][0]).not.toHaveProperty("login");
    expect(info.read_on_mac).toEqual({ provider: "github", host: "github.com", login: "octo" });
    expect(info.provider).toBe("github");
    expect(info.capabilities?.account_switching).toBe(false);
  });

  it("refuses an answer the shell says was read as a changed account", async () => {
    githubShell(() => ({
      ok: false,
      error: { code: "account_changed", message: "The GitHub account changed." },
    }));
    const target = (await desktopPullRequestTarget(MAC_GITHUB_PR))!;
    const advanced: (readonly unknown[])[] = [];
    const stop = onMacReadScopeAdvanced((scope) => advanced.push(scope));
    await expect(fetchDesktopPullRequestChangedFiles(target)).rejects.toMatchObject({
      code: "account_changed",
    });
    // The scope advances, dropping what was read as the previous account.
    expect(advanced).toEqual([readScope(MAC_GITHUB_PR)]);
    stop();
  });

  it("drops an answer that arrives after the workspace user switched", async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    githubShell(async () => {
      await held;
      return { ok: true, data: { data: [], has_more: false }, identity: identity("octo", 1) };
    });
    const target = (await desktopPullRequestTarget(MAC_GITHUB_PR))!;
    const query = (
      window as unknown as { omnigentDesktop: { codeHostQuery: ReturnType<typeof vi.fn> } }
    ).omnigentDesktop.codeHostQuery;
    const pending = fetchDesktopPullRequestChangedFiles(target);
    currentUser.id = "user-2";
    release();
    await expect(pending).rejects.toMatchObject({ code: "account_changed" });
    // Never read again as the new user under the previous user's key.
    expect(query).toHaveBeenCalledTimes(1);
    currentUser.id = "user-1";
  });

  it("reads again once when list stats saw another reader while this read ran", async () => {
    const scope = macReadScopeKey("github", "github.com");
    const query = githubShell(() => {
      if (query.mock.calls.length === 1) {
        // List stats keep octo, then a newer one reads as hubot.
        acceptMacRead(beginMacRead(scope), "octo");
        acceptMacRead(beginMacRead(scope), "hubot");
        return { ok: true, data: { data: [], has_more: false }, identity: identity("octo", 1) };
      }
      return { ok: true, data: { data: [], has_more: false }, identity: identity("hubot", 1) };
    });
    const target = (await desktopPullRequestTarget(MAC_GITHUB_PR))!;
    await expect(fetchDesktopPullRequestChangedFiles(target)).resolves.toBeDefined();
    expect(query).toHaveBeenCalledTimes(2);
  });

  it("names the failing read's host on sign-in errors, for recovery", async () => {
    githubShell(() => ({
      ok: false,
      error: { code: "signed_out", message: "No GitHub account for github.com is signed in." },
    }));
    const target = (await desktopPullRequestTarget(MAC_GITHUB_PR))!;
    const error = await fetchDesktopPullRequestDiff(target).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(DesktopPullRequestError);
    expect(error).toMatchObject({ code: "signed_out", provider: "github", host: "github.com" });
  });
});

describe("a Mac read of a PR the host linked", () => {
  it("keeps the PR selected though only this Mac's links are listed", async () => {
    const MAC_GITHUB_PR = "https://github.com/example-org/universe/pull/42";
    installShell({
      codeHostQuery: async () => ({
        ok: true,
        data: { repository: "example-org/universe", pr: { ...OVERVIEW.pr, url: MAC_GITHUB_PR } },
        identity: { host: "github.com", login: "octo", generation: 1 },
      }),
      codeHostAccounts: { get: async () => null, set: async () => null, onChanged: () => () => {} },
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true }],
      }),
    });
    githubMacReadsPreference.write("on");
    const info = await fetchPullRequestInfo(SESSION, MAC_GITHUB_PR);
    expect(info.selected_pr_url).toBe(MAC_GITHUB_PR);
    expect(info.pr?.url).toBe(MAC_GITHUB_PR);
    githubMacReadsPreference.write("automatic");
  });
});

describe("a PR's commits on this Mac", () => {
  const SHA = "1".repeat(40);
  const COMMIT = {
    sha: SHA,
    subject: "Add a",
    author_login: "ann",
    author_name: "Ann",
    authored_at: "2026-01-02T00:00:00Z",
    committed_at: "2026-01-03T00:00:00Z",
    url: null,
    ci_state: null,
  };
  const LIST = {
    total_count: 1,
    commits: [COMMIT],
    head_sha: HEAD,
    base_sha: BASE,
    complete: true,
  };
  const commitShell = (answers: Record<string, unknown>) =>
    capableShell(answers, [...GITSPOKE_READ_OPERATIONS, "pull_request_commits", "commit_diff"]);
  const EXPECTED_COMMIT = {
    object: "session.github.commit",
    sha: SHA,
    title: "Add a",
    author: "Ann",
    date: "2026-01-02T00:00:00Z",
  };

  it("maps the shell's list to the commits the host lists, at the head the shell names", async () => {
    commitShell({ pull_request_commits: LIST });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    expect(await fetchDesktopPullRequestCommits(target, HEAD)).toEqual({
      object: "list",
      has_more: false,
      data: [EXPECTED_COMMIT],
    });
  });

  it("refuses a list at another head than the panel shows", async () => {
    commitShell({ pull_request_commits: { ...LIST, head_sha: "c".repeat(40) } });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    const error = await fetchDesktopPullRequestCommits(target, HEAD).catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(DesktopPullRequestError);
    expect(error).toMatchObject({ code: "pull_request_changed" });
  });

  it("refuses an older shell's list that names no head", async () => {
    commitShell({ pull_request_commits: { total_count: 1, commits: [COMMIT], complete: true } });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    await expect(fetchDesktopPullRequestCommits(target, HEAD)).rejects.toMatchObject({
      code: "pull_request_changed",
    });
  });

  it.each([
    ["a partial list", { ...LIST, complete: false }],
    ["a list that doesn't say whether it is complete", { ...LIST, complete: undefined }],
  ])("keeps %s as partial, so more commits remain", async (_label, answer) => {
    commitShell({ pull_request_commits: answer });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    expect(await fetchDesktopPullRequestCommits(target, HEAD)).toMatchObject({
      has_more: true,
      data: [EXPECTED_COMMIT],
    });
  });

  it("passes the shell's unsupported classification through", async () => {
    commitShell({ pull_request_commits: { ok: false, error: { code: "unsupported" } } });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    await expect(fetchDesktopPullRequestCommits(target, HEAD)).rejects.toMatchObject({
      code: "unsupported",
    });
  });

  it("maps one commit's patch, read by its full id", async () => {
    const query = commitShell({ commit_diff: { sha: SHA, patch: "diff --git a/a b/a\n" } });
    const target = (await desktopPullRequestTarget(SPOKE_PR))!;
    expect(await fetchDesktopPullRequestCommitDiff(target, SHA)).toEqual({
      object: "session.github.pr_diff",
      patch: "diff --git a/a b/a\n",
    });
    expect(query).toHaveBeenLastCalledWith(
      expect.objectContaining({
        operation: "commit_diff",
        params: { repository: target.repository, sha: SHA },
      }),
    );
  });

  it.each([
    ["both commit reads", ["pull_request_commits", "commit_diff"], true],
    ["no commit_diff", ["pull_request_commits"], false],
    ["no pull_request_commits", ["commit_diff"], false],
  ])(
    "offers commit history with %s, and makes no host request either way",
    async (_label, commitOperations, expected) => {
      const codeHostQuery = vi.fn(async (request: { operation: string }) => ({
        ok: true,
        data: request.operation === "pull_request_overview" ? OVERVIEW : undefined,
      }));
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      installShell({
        codeHostQuery,
        getDesktopFeatures: async () => ({
          codeHosts: [
            { id: "gitspoke", operations: [...GITSPOKE_READ_OPERATIONS, ...commitOperations] },
          ],
        }),
      });
      const target = (await desktopPullRequestTarget(SPOKE_PR))!;
      const info = await fetchDesktopPullRequestInfo(target, Promise.resolve([]));
      expect(info.capabilities?.commit_history).toBe(expected);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
});
