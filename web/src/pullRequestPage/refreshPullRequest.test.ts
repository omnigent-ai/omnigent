// Refreshing one pull request reads it again, and only it, and is done only once what the user
// is looking at has been read again: each open reader, at the revision the fresh overview names.

import { readsFresh } from "@/lib/pullRequestFreshReads";
import { hashKey, QueryClient, QueryObserver, type QueryKey } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearHostTransportRecovery,
  exhaustedHostTransportError,
  recordHostTransportFailure,
  resetHostTransportRecovery,
} from "@/lib/hostTransportRecovery";

import { publishMergeReadinessRequirements } from "@/hooks/useMergeReadiness";
import {
  exampleMergeReadinessSource,
  withPullRequestIntegrations,
} from "@/test/pullRequestIntegrations";
import {
  commitDiffQueryKey,
  pullRequestCommitsQueryKey,
  showCommitDiff,
} from "@/hooks/usePullRequestCommits";
import { pullRequestNativeStackQueryKey } from "@/hooks/usePullRequestNativeStack";

import {
  onPullRequestRefreshRequested,
  refreshPullRequest,
  setPullRequestRefreshSurface,
} from "./refreshPullRequest";

const SESSION = "conv_1";
const PR = "https://github.com/acme/app/pull/7";
const OTHER_PR = "https://github.com/acme/app/pull/8";
const TARGET = { conversationId: SESSION, prUrl: PR };

// A merge readiness source whose readers the tests mount by hand, as the page's hook would.
withPullRequestIntegrations({
  mergeReadiness: exampleMergeReadinessSource(async () => ({ kind: "checking" })),
});

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: Error) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const BASE = "b".repeat(40);
const REVISION_H1 = `${BASE}:h1`;
const overviewOf = (head: string, base = BASE) => ({
  pr: { url: PR, head_sha: head, base_sha: base },
});

let unsubscribes: (() => void)[] = [];

/** A mounted reader of `queryKey`: an observer subscribed as a component would. */
function mount(client: QueryClient, queryKey: QueryKey, queryFn: () => Promise<unknown>) {
  const observer = new QueryObserver(client, { queryKey, queryFn, retry: false });
  const unsubscribe = observer.subscribe(() => {});
  unsubscribes.push(unsubscribe);
  return unsubscribe;
}

/**
 * A merge readiness reader of `prUrl`, as the page's hook: it reads GitHub's answer, and only
 * on a later render applies it, reading the checks `checksFor` says that answer needs.
 */
function mountReadiness(
  client: QueryClient,
  prUrl: string,
  github: () => Promise<unknown>,
  checksFor: (answer: unknown) => { head: string; read: () => Promise<unknown> } | null = () =>
    null,
) {
  const githubKey = ["example-merge-readiness", prUrl, "readiness"];
  const reader = Symbol("readiness reader");
  const observer = new QueryObserver(client, {
    queryKey: githubKey,
    queryFn: github,
    retry: false,
  });
  let checks: { hash: string; stop: () => void } | null = null;
  let render: ReturnType<typeof setTimeout> | undefined;
  const apply = () => {
    const state = observer.getCurrentResult();
    const wanted = state.status === "success" ? checksFor(state.data) : null;
    const key = wanted ? ["example-merge-readiness", prUrl, "checks", wanted.head] : null;
    if (checks && (!key || checks.hash !== hashKey(key))) {
      checks.stop();
      checks = null;
    }
    if (key && wanted && !checks)
      checks = { hash: hashKey(key), stop: mount(client, key, wanted.read) };
    publishMergeReadinessRequirements(
      reader,
      new Map([
        [
          hashKey(githubKey),
          {
            appliedAt: Math.max(state.dataUpdatedAt, state.errorUpdatedAt),
            checks: checks?.hash ?? null,
          },
        ],
      ]),
    );
  };
  const stop = observer.subscribe(() => {
    clearTimeout(render);
    render = setTimeout(apply, 30);
  });
  const unmount = () => {
    clearTimeout(render);
    stop();
    checks?.stop();
    publishMergeReadinessRequirements(reader, null);
  };
  unsubscribes.push(unmount);
  return unmount;
}

const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 20);
  });

/** Whether `promise` is still pending after the event loop settles. */
async function pending(promise: Promise<unknown>): Promise<boolean> {
  let done = false;
  void promise.then(() => (done = true));
  await settle();
  return !done;
}

afterEach(() => {
  for (const unsubscribe of unsubscribes) unsubscribe();
  unsubscribes = [];
  resetHostTransportRecovery();
});

describe("what a refresh reads", () => {
  it("reads every reader of this PR again, and no other PR's, session's or list's", async () => {
    const client = new QueryClient();
    const reads: string[] = [];
    const reader = (name: string, data: unknown) => async () => {
      reads.push(name);
      return data;
    };
    mount(client, ["pr-page-github-info", SESSION, PR], reader("overview", overviewOf("h1")));
    mount(
      client,
      ["pr-page-github-review-threads", SESSION, PR, "", REVISION_H1],
      reader("threads", []),
    );
    mount(
      client,
      ["pr-page-github-merge-options", SESSION, PR, "mac-read"],
      reader("merge-options", {}),
    );
    mount(client, ["pr-page-github-info", SESSION, OTHER_PR], reader("other PR", overviewOf("h9")));
    mount(client, ["pr-page-github-info", "conv_2", PR], reader("other session", overviewOf("h1")));
    mount(client, ["pull-requests-page", "inbox"], reader("inbox", []));
    mountReadiness(client, PR, reader("readiness", { ok: true }));
    mountReadiness(client, OTHER_PR, reader("other readiness", { ok: true }));
    mount(
      client,
      pullRequestNativeStackQueryKey(PR, SESSION, "h1"),
      reader("native stack", { read: null, members: null }),
    );
    // Another session's stack read fails: a refresh here must neither read it nor wait for it.
    mount(client, pullRequestNativeStackQueryKey(PR, "conv_2", "h1"), async () => {
      reads.push("other session's native stack");
      throw new Error("other host unavailable");
    });
    mount(client, pullRequestNativeStackQueryKey(OTHER_PR, SESSION, "h9"), async () => {
      reads.push("other PR's native stack");
      return { read: null, members: null };
    });
    await settle();
    reads.length = 0;

    expect(await refreshPullRequest(client, TARGET)).toEqual({ kind: "ok" });
    expect(reads.sort()).toEqual([
      "merge-options",
      "native stack",
      "overview",
      "readiness",
      "threads",
    ]);
  });

  it("reads its PR fresh while it runs, past what this Mac kept, and not after", async () => {
    const client = new QueryClient();
    const freshDuringRead: boolean[] = [];
    let reading = false;
    mount(client, ["pr-page-github-info", SESSION, PR], async () => {
      if (reading) freshDuringRead.push(readsFresh(PR), readsFresh(OTHER_PR));
      return overviewOf("h1");
    });
    await settle();
    reading = true;
    expect(await refreshPullRequest(client, TARGET)).toEqual({ kind: "ok" });
    expect(freshDuringRead).toEqual([true, false]);
    expect(readsFresh(PR)).toBe(false);
  });

  it("starts a used-up host episode again for this PR's overview only", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    mount(client, ["pr-page-github-info", SESSION, OTHER_PR], async () => overviewOf("h9"));
    await settle();
    const hashOf = (url: string) =>
      client.getQueryCache().find({ queryKey: ["pr-page-github-info", SESSION, url], exact: true })!
        .queryHash;
    const transport = Object.assign(new Error("fetch failed"), { name: "TypeError" });
    for (const url of [PR, OTHER_PR]) {
      for (let attempt = 0; attempt < 20 && !exhaustedHostTransportError(hashOf(url)); attempt++) {
        recordHostTransportFailure(hashOf(url), SESSION, transport);
      }
      expect(exhaustedHostTransportError(hashOf(url))).toBeDefined();
    }
    await refreshPullRequest(client, TARGET);
    expect(exhaustedHostTransportError(hashOf(PR))).toBeUndefined();
    expect(exhaustedHostTransportError(hashOf(OTHER_PR))).toBeDefined();
    clearHostTransportRecovery(hashOf(OTHER_PR));
  });
});

describe("when a refresh is done", () => {
  it("waits for the reader of the fresh head, mounted after the overview answers", async () => {
    const client = new QueryClient();
    let head = "h1";
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    const oldCommits = mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h1"], async () => [
      "c1",
    ]);
    await settle();

    head = "h2";
    const refresh = refreshPullRequest(client, TARGET);
    // The old head's reader is read again and settles first; that isn't what the user will see.
    expect(await pending(refresh)).toBe(true);
    // React replaces the reader later than the next macrotask.
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });
    oldCommits();
    const fresh = deferred<string[]>();
    mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h2"], () => fresh.promise);
    expect(await pending(refresh)).toBe(true);
    fresh.resolve(["c2"]);
    expect(await refresh).toEqual({ kind: "ok" });
  });

  it("fails with the fresh head's reader's failure", async () => {
    const client = new QueryClient();
    let head = "h1";
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    const oldCommits = mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h1"], async () => [
      "c1",
    ]);
    await settle();

    head = "h2";
    const refresh = refreshPullRequest(client, TARGET);
    await settle();
    oldCommits();
    const fresh = deferred<string[]>();
    mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h2"], () => fresh.promise);
    fresh.reject(new Error("GitHub didn't answer."));
    const result = await refresh;
    expect(result.kind).toBe("failed");
    expect(result.kind === "failed" && result.error.message).toBe("GitHub didn't answer.");
  });

  it("fails with the overview's own failure", async () => {
    const client = new QueryClient();
    let fail = false;
    mount(client, ["pr-page-github-info", SESSION, PR], async () => {
      if (fail) throw new Error("Couldn't read the pull request.");
      return overviewOf("h1");
    });
    await settle();
    fail = true;
    const result = await refreshPullRequest(client, TARGET);
    expect(result.kind === "failed" && result.error.message).toBe(
      "Couldn't read the pull request.",
    );
  });

  it("times out, rather than succeeding, when the overview never answers", async () => {
    const client = new QueryClient();
    let hang = false;
    mount(client, ["pr-page-github-info", SESSION, PR], () =>
      hang ? new Promise(() => {}) : Promise.resolve(overviewOf("h1")),
    );
    await settle();
    hang = true;
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 300 })).toEqual({
      kind: "timed-out",
    });
  });

  it("times out when a reader the user is looking at never answers", async () => {
    const client = new QueryClient();
    let hang = false;
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    mount(client, ["pr-page-github-review-threads", SESSION, PR, "", REVISION_H1], () =>
      hang ? new Promise(() => {}) : Promise.resolve([]),
    );
    await settle();
    hang = true;
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 300 })).toEqual({
      kind: "timed-out",
    });
  });

  it("stops waiting for a reader the user closed, once the details show something else", async () => {
    const client = new QueryClient();
    const owner = { id: Symbol("details") };
    setPullRequestRefreshSurface(owner, { prUrl: PR, showingDetails: true, tab: "changes" });
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    const threads = mount(
      client,
      ["pr-page-github-review-threads", SESSION, PR, "", REVISION_H1],
      () => Promise.resolve([]),
    );
    await settle();
    const refresh = refreshPullRequest(client, TARGET, { owner });
    threads();
    // Gone while the details still show the same tab: being replaced, so waited for.
    expect(await pending(refresh)).toBe(true);
    setPullRequestRefreshSurface(owner, { prUrl: PR, showingDetails: true, tab: "summary" });
    expect(await refresh).toEqual({ kind: "ok" });
    setPullRequestRefreshSurface(owner, null);
  });

  it("waits through a reader's replacement however long it takes, until the new one answers", async () => {
    const client = new QueryClient();
    let head = "h1";
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    const old = mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h1"], async () => []);
    await settle();
    head = "h2";
    const refresh = refreshPullRequest(client, TARGET);
    old();
    await new Promise((resolve) => {
      setTimeout(resolve, 400);
    });
    expect(await pending(refresh)).toBe(true);
    const fresh = deferred<unknown[]>();
    mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h2"], () => fresh.promise);
    expect(await pending(refresh)).toBe(true);
    fresh.reject(new Error("The new head's commits failed."));
    const result = await refresh;
    expect(result.kind === "failed" && result.error.message).toBe("The new head's commits failed.");
  });

  it("takes only the threads of the fresh revision as read again", async () => {
    const client = new QueryClient();
    let head = "h1";
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    const old = mount(
      client,
      ["pr-page-github-review-threads", SESSION, PR, "alice", REVISION_H1],
      async () => [],
    );
    await settle();
    head = "h2";
    const refresh = refreshPullRequest(client, TARGET);
    expect(await pending(refresh)).toBe(true);
    old();
    mount(
      client,
      ["pr-page-github-review-threads", SESSION, PR, "alice", `${BASE}:h2`],
      async () => [],
    );
    expect(await refresh).toEqual({ kind: "ok" });
  });

  it("waits for both halves of merge readiness, the fresh head's checks included", async () => {
    const client = new QueryClient();
    let head = "h1";
    const checks = deferred<unknown[]>();
    let checksAsked = false;
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    mountReadiness(
      client,
      PR,
      async () => ({ head_sha: head }),
      (answer) => {
        const answered = (answer as { head_sha: string }).head_sha;
        const read = () => {
          checksAsked = true;
          return checks.promise;
        };
        return { head: answered, read: answered === "h2" ? read : async () => [] };
      },
    );
    await settle();
    await settle();
    head = "h2";
    const refresh = refreshPullRequest(client, TARGET);
    await vi.waitFor(() => expect(checksAsked).toBe(true));
    expect(await pending(refresh)).toBe(true);
    checks.reject(new Error("DevPortal didn't answer."));
    const result = await refresh;
    expect(result.kind === "failed" && result.error.message).toBe("DevPortal didn't answer.");
  });

  it("leaves nothing watching once it times out", async () => {
    const client = new QueryClient();
    let head = "h1";
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    mount(client, ["pr-page-github-pr-commits", SESSION, PR, "h1"], async () => []);
    await settle();
    const cache = client.getQueryCache();
    const subscribe = cache.subscribe.bind(cache);
    let watching = 0;
    cache.subscribe = (listener) => {
      watching += 1;
      const stop = subscribe(listener);
      return () => {
        watching -= 1;
        stop();
      };
    };
    head = "h2";
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 80 })).toEqual({
      kind: "timed-out",
    });
    expect(watching).toBe(0);
  });

  it("doesn't wait for readers nothing shows, such as a closed picker", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    // Cached, with no observer: read when a picker last opened.
    client.setQueryData(["pr-page-github-reviewers", SESSION, PR], []);
    await settle();
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 2_000 })).toEqual({ kind: "ok" });
  });

  it("waits for a part of the page that holds its own state, and takes its failure", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    await settle();
    const comparison = deferred<{ kind: "failed"; error: Error }>();
    const stop = onPullRequestRefreshRequested(
      { prUrl: PR, kind: "comparison", client, owner: null },
      () => comparison.promise,
    );
    try {
      const refresh = refreshPullRequest(client, TARGET);
      expect(await pending(refresh)).toBe(true);
      comparison.resolve({ kind: "failed", error: new Error("Couldn't compare.") });
      const result = await refresh;
      expect(result.kind === "failed" && result.error.message).toBe("Couldn't compare.");
    } finally {
      stop();
    }
  });

  it("gives the stack's members their turn before anything compares against them", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    await settle();
    const order: string[] = [];
    const members = deferred<{ kind: "ok" }>();
    const stopMembers = onPullRequestRefreshRequested(
      { prUrl: PR, kind: "members", client, owner: null },
      () => {
        order.push("members started");
        return members.promise.then((result) => {
          order.push("members read");
          return result;
        });
      },
    );
    const stopComparison = onPullRequestRefreshRequested(
      { prUrl: PR, kind: "comparison", client, owner: null },
      async (attempt) => {
        await attempt.members;
        order.push("comparison after members");
        return { kind: "ok" };
      },
    );
    try {
      const refresh = refreshPullRequest(client, TARGET);
      await settle();
      members.resolve({ kind: "ok" });
      expect(await refresh).toEqual({ kind: "ok" });
      expect(order).toEqual(["members started", "members read", "comparison after members"]);
    } finally {
      stopMembers();
      stopComparison();
    }
  });
});

describe("readers a refresh doesn't wait for", () => {
  it("skips a mounted reader that's disabled", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    const observer = new QueryObserver(client, {
      queryKey: ["pr-page-github-merge-options", SESSION, PR],
      queryFn: async () => ({}),
      enabled: false,
    });
    unsubscribes.push(observer.subscribe(() => {}));
    await settle();
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 2_000 })).toEqual({ kind: "ok" });
  });
});

describe("whose parts a refresh reaches", () => {
  it("reaches only the parts of the details that asked, with the same client", async () => {
    const client = new QueryClient();
    const otherClient = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    await settle();
    const mine = { id: Symbol("these details") };
    const other = { id: Symbol("another session's details") };
    const reached: string[] = [];
    const part = (name: string) => async () => {
      reached.push(name);
      return { kind: "ok" as const };
    };
    const stops = [
      onPullRequestRefreshRequested(
        { prUrl: PR, kind: "comparison", client, owner: mine },
        part("mine"),
      ),
      onPullRequestRefreshRequested(
        { prUrl: PR, kind: "comparison", client, owner: other },
        part("other session"),
      ),
      onPullRequestRefreshRequested(
        { prUrl: PR, kind: "comparison", client: otherClient, owner: mine },
        part("other client"),
      ),
      onPullRequestRefreshRequested(
        { prUrl: OTHER_PR, kind: "comparison", client, owner: mine },
        part("other PR"),
      ),
    ];
    try {
      expect(await refreshPullRequest(client, TARGET, { owner: mine })).toEqual({ kind: "ok" });
      expect(reached).toEqual(["mine"]);
    } finally {
      for (const stop of stops) stop();
    }
  });

  it("tells its parts when it's over, so a late one stops", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    await settle();
    let aborted = false;
    const stop = onPullRequestRefreshRequested(
      { prUrl: PR, kind: "comparison", client, owner: null },
      (attempt) =>
        new Promise((resolve) => {
          attempt.signal.addEventListener("abort", () => {
            aborted = true;
            resolve({ kind: "timed-out" });
          });
        }),
    );
    try {
      expect(await refreshPullRequest(client, TARGET, { deadlineMs: 100 })).toEqual({
        kind: "timed-out",
      });
      expect(aborted).toBe(true);
    } finally {
      stop();
    }
  });
});

describe("the Commits tab's reads", () => {
  const source = (head: string) => ({
    account: { host: "github.com", login: "octo" },
    repository: "acme/app",
    number: 7,
    headSha: head,
    baseSha: BASE,
  });

  it("reads a failed commit list again at the same head, and waits for it", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    let fail = true;
    mount(client, pullRequestCommitsQueryKey(source("h1")), async () => {
      if (fail) throw new Error("No commits.");
      return { commits: [] };
    });
    await settle();
    fail = false;
    expect(await refreshPullRequest(client, TARGET)).toEqual({ kind: "ok" });
    expect(client.getQueryState(pullRequestCommitsQueryKey(source("h1")))?.status).toBe("success");
  });

  it("waits for the commit list of a new head, and takes its failure", async () => {
    const client = new QueryClient();
    let head = "h1";
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf(head));
    const old = mount(client, pullRequestCommitsQueryKey(source("h1")), async () => ({
      commits: [],
    }));
    await settle();
    head = "h2";
    const refresh = refreshPullRequest(client, TARGET);
    expect(await pending(refresh)).toBe(true);
    old();
    const fresh = deferred<unknown>();
    mount(client, pullRequestCommitsQueryKey(source("h2")), () => fresh.promise);
    expect(await pending(refresh)).toBe(true);
    fresh.reject(new Error("The new head's commits failed."));
    const result = await refresh;
    expect(result.kind === "failed" && result.error.message).toBe("The new head's commits failed.");
  });

  /** A commit diff on screen in `owner`'s details for `of`'s pull request, as its view shows it. */
  function showDiff(
    client: QueryClient,
    of: ReturnType<typeof source>,
    sha: string,
    read: () => Promise<unknown>,
    owner: { id: symbol } | null = null,
  ) {
    const key = commitDiffQueryKey(of, sha);
    mount(client, key, read);
    unsubscribes.push(showCommitDiff(client, of, sha, owner));
    return key;
  }

  it("keeps a commit's diff it already read, and reads a failed one again", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    let diffReads = 0;
    showDiff(client, source("h1"), "c1", async () => {
      diffReads += 1;
      return { patch: "" };
    });
    let failing = true;
    const failed = showDiff(client, source("h1"), "c2", async () => {
      if (failing) throw new Error("No diff.");
      return { patch: "" };
    });
    await settle();
    failing = false;
    const readsBefore = diffReads;
    expect(await refreshPullRequest(client, TARGET)).toEqual({ kind: "ok" });
    expect(diffReads).toBe(readsBefore);
    expect(client.getQueryState(failed)?.status).toBe("success");
  });

  it("reads a shown commit's failed diff again when the PR's history doesn't list it", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    // GitHub's history stops short of the PR's 300 commits; the open one isn't in it.
    mount(client, pullRequestCommitsQueryKey(source("h1")), async () => ({
      commits: [{ sha: "listed" }],
      total_count: 300,
      complete: false,
    }));
    let failing = true;
    let reads = 0;
    const shown = showDiff(client, source("h1"), "unlisted", async () => {
      reads += 1;
      if (failing) throw new Error("No diff.");
      return { patch: "" };
    });
    await settle();
    failing = false;
    reads = 0;
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 1_000 })).toEqual({ kind: "ok" });
    expect(reads).toBe(1);
    expect(client.getQueryState(shown)?.status).toBe("success");
  });

  it("leaves another PR's commit diffs in the same repository alone, and another view's", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    let otherReads = 0;
    const failing = (name: string) => async () => {
      otherReads += 1;
      throw new Error(`${name} failed.`);
    };
    // Pull request #8 of the same repository, open in another session's details.
    const otherPr = { ...source("h8"), number: 8 };
    const otherDetails = { id: Symbol("other details") };
    mount(client, ["pr-page-github-info", "conv_2", OTHER_PR], async () => overviewOf("h8"));
    showDiff(client, otherPr, "c8", failing("#8's diff"), otherDetails);
    // This PR's own commit, shown elsewhere as another account.
    const otherAccount = { ...source("h1"), account: { host: "github.com", login: "other" } };
    showDiff(client, otherAccount, "c1", failing("Another account's diff"), otherDetails);
    // A diff read nothing shows for this PR, though it's in this PR's repository.
    mount(client, commitDiffQueryKey(source("h1"), "c9"), failing("An unshown diff"));
    showDiff(client, source("h1"), "c1", async () => ({ patch: "" }));
    await settle();
    otherReads = 0;
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 1_000 })).toEqual({ kind: "ok" });
    expect(otherReads).toBe(0);
  });
});

describe("readers the fresh answers show", () => {
  /** A draft PR made ready: its readiness reader enables checks only once it renders that. */
  async function checksEnabledDuringRefresh(outcome: "succeed" | "fail") {
    const client = new QueryClient();
    let ready = false;
    const checks = deferred<unknown[]>();
    let checksAsked = false;
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    mountReadiness(
      client,
      PR,
      async () => ({ state: ready ? "OPEN" : "DRAFT" }),
      (answer) =>
        (answer as { state: string }).state === "OPEN"
          ? {
              head: "h1",
              read: () => {
                checksAsked = true;
                return checks.promise;
              },
            }
          : null,
    );
    await settle();
    await settle();
    ready = true;
    let ended = false;
    const refresh = refreshPullRequest(client, TARGET).then((result) => {
      ended = true;
      return result;
    });
    await vi.waitFor(() => expect(checksAsked).toBe(true));
    expect(ended).toBe(false);
    if (outcome === "succeed") checks.resolve([]);
    else checks.reject(new Error("DevPortal didn't answer."));
    return refresh;
  }

  it("waits for checks the fresh answer enabled, and takes their success", async () => {
    expect(await checksEnabledDuringRefresh("succeed")).toEqual({ kind: "ok" });
  });

  it("waits for checks the fresh answer enabled, and takes their failure", async () => {
    const result = await checksEnabledDuringRefresh("fail");
    expect(result.kind === "failed" && result.error.message).toBe("DevPortal didn't answer.");
  });

  it("waits for a readiness reader that hasn't rendered yet", async () => {
    const client = new QueryClient();
    mount(client, ["pr-page-github-info", SESSION, PR], async () => overviewOf("h1"));
    // A GitHub readiness read with no reader applying it yet, as before the hook's first render.
    mount(client, ["example-merge-readiness", PR, "readiness"], async () => ({}));
    await settle();
    expect(await refreshPullRequest(client, TARGET, { deadlineMs: 300 })).toEqual({
      kind: "timed-out",
    });
  });
});

describe("leaving the pull request while it refreshes", () => {
  it("settles on the refresh's own overview read, not a timeout", async () => {
    const client = new QueryClient();
    const owner = { id: Symbol("details") };
    setPullRequestRefreshSurface(owner, { prUrl: PR, showingDetails: true, tab: "summary" });
    const answer = deferred<unknown>();
    let first = true;
    const overview = mount(client, ["pr-page-github-info", SESSION, PR], () => {
      if (first) {
        first = false;
        return Promise.resolve(overviewOf("h1"));
      }
      return answer.promise;
    });
    await settle();
    const refresh = refreshPullRequest(client, TARGET, { owner, deadlineMs: 3_000 });
    await settle();
    // The user opens another PR: this one's overview reader goes, and its read finishes later.
    setPullRequestRefreshSurface(owner, { prUrl: OTHER_PR, showingDetails: true, tab: "summary" });
    overview();
    answer.resolve(overviewOf("h1"));
    expect(await refresh).toEqual({ kind: "ok" });
    setPullRequestRefreshSurface(owner, null);
  });
});
