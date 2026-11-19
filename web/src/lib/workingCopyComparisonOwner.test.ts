import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ComparisonWorkspace, RevisionSpec } from "@/lib/customComparison";
import type { BranchPointComparison, BranchPointListing } from "@/lib/workingCopyComparison";
import type { WorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";
import { watchWorkspaceGitRoute } from "@/lib/workingCopyComparisons";
import {
  DEFAULT_WORKSPACE_SIDEBAR_SOURCE,
  setWorkspaceSidebarSourceForTests,
} from "@/lib/workspaceSidebarSource";

import {
  ATTENTION_MIN_INTERVAL_MS,
  type ComparisonConsumer,
  CONNECT_FALLBACK_MS,
  IDLE_GRACE_MS,
  IDLE_TTL_MS,
  POLL_MS,
  SLOW_LIST_MS,
  SLOW_POLL_MS,
  WorkingCopyComparisonOwner,
} from "./workingCopyComparisonOwner";
import type { WorkspaceGitRoute } from "./workingCopyComparison";

const SESSION = "conv_1";
const AVAILABLE: WorkspaceGitRoute = { state: "served" };

function comparison(id: string, entries: [path: string, fingerprint: string][] = []) {
  return {
    id,
    ref: "origin/main",
    baseOid: "b".repeat(40),
    headOid: "h".repeat(40),
    refOid: "r".repeat(40),
    entries: entries.map(([path, fingerprint]) => ({
      path,
      name: path,
      status: "modified" as const,
      bytes: 1,
      modified_at: null,
      lines_added: 1,
      lines_removed: 0,
      change: null,
      baseKind: "file" as const,
      workingKind: "file" as const,
      binary: false,
      problem: null,
      fingerprint,
    })),
    unrepresentableNames: 0,
    listedAt: 0,
  } satisfies BranchPointComparison;
}

interface PendingList {
  refresh: boolean;
  signal: AbortSignal;
  spec: RevisionSpec | null;
  resolve: (listing: BranchPointListing) => void;
  reject: (error: unknown) => void;
}

const REPO: ComparisonWorkspace = { hostId: "host_1", root: "/repo" };
const typedKey = (sessionId: string, workspace: ComparisonWorkspace = REPO) =>
  JSON.stringify([sessionId, workspace.hostId, workspace.root]);

/** An owner over fakes: lists wait until the test answers them. */
function harness({ route = AVAILABLE as WorkspaceGitRoute | null } = {}) {
  const lists: PendingList[] = [];
  const watches: {
    onRoute: (route: WorkspaceGitRoute) => void;
    stop: () => void;
    ask: () => void;
  }[] = [];
  let intent: WorkingCopyBaseIntent = "automatic";
  const intentListeners = new Set<() => void>();
  const attentionListeners = new Set<() => void>();
  const accessListeners = new Set<(sessionId: string) => void>();
  let visible = true;
  const typed = new Map<string, RevisionSpec>();
  const typedListeners = new Set<() => void>();
  const forgotten: string[] = [];
  const owner = new WorkingCopyComparisonOwner({
    forgetListings: (sessionId) => forgotten.push(sessionId),
    fetchListing: (_sessionId, { refresh, signal, spec }) =>
      new Promise((resolve, reject) => {
        lists.push({ refresh, signal, spec, resolve, reject });
      }),
    watchRoute: (_target, onRoute) => {
      const watch = { onRoute, stop: vi.fn(), ask: vi.fn() };
      watches.push(watch);
      if (route) queueMicrotask(() => onRoute(route));
      return watch;
    },
    readIntent: () => intent,
    subscribeIntent: (listener) => {
      intentListeners.add(listener);
      return () => intentListeners.delete(listener);
    },
    now: () => Date.now(),
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    documentVisible: () => visible,
    subscribeAttention: (listener) => {
      attentionListeners.add(listener);
      return () => attentionListeners.delete(listener);
    },
    subscribeAccessReady: (listener) => {
      accessListeners.add(listener);
      return () => accessListeners.delete(listener);
    },
    customComparisons: {
      read: (sessionId, workspace) => typed.get(typedKey(sessionId, workspace)) ?? null,
      write: (sessionId, workspace, spec) => typed.set(typedKey(sessionId, workspace), spec),
      clear: (sessionId, workspace) => typed.delete(typedKey(sessionId, workspace)),
      subscribe: (listener) => {
        typedListeners.add(listener);
        return () => typedListeners.delete(listener);
      },
    },
  });
  return {
    owner,
    lists,
    watches,
    typed,
    forgotten,
    /** Another window saved or cleared a typed comparison. */
    typedElsewhere(sessionId: string, spec: RevisionSpec | null) {
      if (spec) typed.set(typedKey(sessionId), spec);
      else typed.delete(typedKey(sessionId));
      for (const listener of typedListeners) listener();
    },
    open: (pending: PendingList[]) => pending.filter((list) => !list.signal.aborted),
    setIntent(next: WorkingCopyBaseIntent) {
      intent = next;
      for (const listener of intentListeners) listener();
    },
    setVisible(next: boolean) {
      visible = next;
      for (const listener of attentionListeners) listener();
    },
    focus() {
      for (const listener of attentionListeners) listener();
    },
    accessReady(sessionId: string) {
      for (const listener of accessListeners) listener(sessionId);
    },
    snapshot: () => owner.snapshot(SESSION),
  };
}

function consumer(kind: ComparisonConsumer["kind"], enabled = true): ComparisonConsumer {
  return { kind, enabled, sessionHostId: "host_1", root: "/repo" };
}

/** Lets the route and list promises settle. */
async function settle() {
  // oxlint-disable-next-line no-await-in-loop -- each step in turn
  for (let index = 0; index < 5; index += 1) await Promise.resolve();
}

async function answer(list: PendingList | undefined, listing: BranchPointListing) {
  expect(list).toBeDefined();
  list!.resolve(listing);
  await settle();
}

const listed = (id: string, entries?: [string, string][]): BranchPointListing => ({
  kind: "comparison",
  comparison: comparison(id, entries),
});

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WorkingCopyComparisonOwner", () => {
  it("lists once for several views and coalesces a burst of invalidations into one follow-up", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    test.owner.register(SESSION, consumer("surface"));
    test.owner.register(SESSION, consumer("count"));
    await settle();
    expect(test.lists).toHaveLength(1);
    expect(test.watches).toHaveLength(1);

    for (let index = 0; index < 5; index += 1) test.owner.invalidate(SESSION);
    test.focus();
    expect(test.lists).toHaveLength(1);
    await answer(test.lists[0], listed("cmp_1"));
    expect(test.lists).toHaveLength(2);
    await answer(test.lists[1], listed("cmp_1"));
    expect(test.lists).toHaveLength(2);
    expect(test.snapshot().established?.id).toBe("cmp_1");
  });

  it("polls only while a visible surface is registered, and resumes when shown", async () => {
    const test = harness();
    const count = test.owner.register(SESSION, consumer("count"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(test.lists).toHaveLength(1);

    const surface = test.owner.register(SESSION, consumer("surface"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(test.lists).toHaveLength(2);
    await answer(test.lists[1], listed("cmp_1"));

    test.setVisible(false);
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(test.lists).toHaveLength(2);
    test.setVisible(true);
    await settle();
    expect(test.lists).toHaveLength(3);
    await answer(test.lists[2], listed("cmp_1"));

    test.owner.unregister(SESSION, surface);
    await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    expect(test.lists).toHaveLength(3);
    test.owner.unregister(SESSION, count);
  });

  it("slows the poll after a slow list or a timeout", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    await vi.advanceTimersByTimeAsync(SLOW_LIST_MS + 1);
    await answer(test.lists[0], listed("cmp_1"));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(test.lists).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(SLOW_POLL_MS - POLL_MS);
    expect(test.lists).toHaveLength(2);

    await answer(test.lists[1], {
      kind: "unavailable",
      problem: { reason: "timeout", transient: true },
    });
    await vi.advanceTimersByTimeAsync(POLL_MS);
    expect(test.lists).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(SLOW_POLL_MS - POLL_MS);
    expect(test.lists).toHaveLength(3);
  });

  it("keeps an established comparison through transient failures and drops it on a definitive one", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    for (const reason of ["timeout", "git_failed"]) {
      test.owner.invalidate(SESSION);
      // oxlint-disable-next-line no-await-in-loop -- each step in turn
      await answer(test.lists.at(-1), {
        kind: "unavailable",
        problem: { reason, transient: true },
      });
      expect(test.snapshot().established?.id).toBe("cmp_1");
      expect(test.snapshot().problem).toEqual({ reason, transient: true });
    }
    // A request the host didn't answer is as transient, and says why.
    test.owner.invalidate(SESSION);
    test.lists.at(-1)!.reject(new Error("connection reset"));
    await settle();
    expect(test.snapshot().established?.id).toBe("cmp_1");
    expect(test.snapshot().problem).toEqual({
      reason: "request_failed",
      transient: true,
      detail: "connection reset",
    });
    test.owner.invalidate(SESSION);
    await answer(test.lists.at(-1), {
      kind: "unavailable",
      problem: { reason: "no_branch_point", transient: false },
    });
    expect(test.snapshot().established).toBeNull();
  });

  it("falls back after the connect clock only under Automatic", async () => {
    const test = harness({ route: { state: "checking" } });
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    expect(test.lists).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(CONNECT_FALLBACK_MS);
    expect(test.snapshot().connectTimedOut).toBe(true);

    test.setIntent("branch-point");
    expect(test.snapshot().connectTimedOut).toBe(false);
    await vi.advanceTimersByTimeAsync(CONNECT_FALLBACK_MS * 2);
    expect(test.snapshot().connectTimedOut).toBe(false);
  });

  it("waits for a view to know the session's host before listing", async () => {
    const test = harness();
    const token = test.owner.register(SESSION, { ...consumer("surface"), sessionHostId: null });
    await settle();
    expect(test.snapshot().support.kind).toBe("checking");
    expect(test.lists).toHaveLength(0);
    test.owner.update(SESSION, token, consumer("surface"));
    await settle();
    expect(test.snapshot().support.kind).toBe("available");
    expect(test.lists).toHaveLength(1);
  });

  it("lists only while a registered view may read the workspace", async () => {
    const test = harness();
    const token = test.owner.register(SESSION, consumer("file", false));
    await settle();
    expect(test.lists).toHaveLength(0);
    test.owner.update(SESSION, token, consumer("file", true));
    await settle();
    expect(test.lists).toHaveLength(1);
    // Losing permission mid-list abandons the list, and its answer is never shown.
    test.owner.update(SESSION, token, consumer("file", false));
    expect(test.lists[0]!.signal.aborted).toBe(true);
    await answer(test.lists[0], listed("cmp_late"));
    expect(test.snapshot().established).toBeNull();
    test.owner.invalidate(SESSION);
    await settle();
    expect(test.lists).toHaveLength(1);
  });

  it("shares the HEAD override across views until a base is chosen", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    test.owner.showLastCommit(SESSION);
    expect(test.lists[0]!.signal.aborted).toBe(true);
    expect(test.snapshot().headOverride).toBe(true);
    test.owner.invalidate(SESSION);
    await settle();
    expect(test.open(test.lists)).toHaveLength(0);

    test.owner.chose(SESSION);
    await settle();
    expect(test.snapshot().headOverride).toBe(false);
    expect(test.open(test.lists)).toHaveLength(1);

    test.owner.showLastCommit(SESSION);
    test.owner.refresh(SESSION);
    expect(test.snapshot().headOverride).toBe(false);
  });

  it("asks for a new comparison on Refresh", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    test.owner.refresh(SESSION);
    await settle();
    expect(test.lists.map((list) => list.refresh)).toEqual([false, true]);
  });

  it("keeps a session's state through a quick remount and forgets it after the idle TTL", async () => {
    const test = harness();
    let token = test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    test.owner.showLastCommit(SESSION);
    test.owner.unregister(SESSION, token);
    await vi.advanceTimersByTimeAsync(IDLE_GRACE_MS / 2);
    token = test.owner.register(SESSION, consumer("surface"));
    expect(test.snapshot().headOverride).toBe(true);
    expect(test.watches[0]!.stop).not.toHaveBeenCalled();

    test.owner.unregister(SESSION, token);
    // Focus while idle doesn't keep the session alive.
    for (let elapsed = 0; elapsed < IDLE_TTL_MS; elapsed += ATTENTION_MIN_INTERVAL_MS) {
      test.focus();
      // oxlint-disable-next-line no-await-in-loop -- each step in turn
      await vi.advanceTimersByTimeAsync(ATTENTION_MIN_INTERVAL_MS);
    }
    expect(test.watches[0]!.stop).toHaveBeenCalled();
    expect(test.snapshot()).toMatchObject({ established: null, headOverride: false });
  });

  it("names a comparison current only for its generation, id, and the file's fingerprint", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("file"));
    await settle();
    await answer(test.lists[0], listed("cmp_1", [["a.ts", "fp1"]]));
    const { generation } = test.snapshot();
    expect(test.owner.isCurrent(SESSION, generation, "cmp_1", "a.ts", "fp1")).toBe(true);
    expect(test.owner.isCurrent(SESSION, generation, "cmp_1", "a.ts", "fp0")).toBe(false);
    expect(test.owner.isCurrent(SESSION, generation, "cmp_0", "a.ts", "fp1")).toBe(false);

    test.owner.invalidate(SESSION);
    await answer(test.lists[1], listed("cmp_1", [["a.ts", "fp2"]]));
    expect(test.owner.isCurrent(SESSION, generation, "cmp_1", "a.ts", "fp1")).toBe(false);

    test.owner.reset();
    expect(test.owner.isCurrent(SESSION, generation, "cmp_1", "a.ts", "fp2")).toBe(false);
  });

  it("drops every session on reset and lists again for the views still registered", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    test.owner.showLastCommit(SESSION);
    const before = test.snapshot().generation;
    test.owner.reset();
    expect(test.snapshot()).toMatchObject({ established: null, headOverride: false });
    expect(test.snapshot().generation).toBe(before + 1);
    await settle();
    expect(test.watches).toHaveLength(2);
    expect(test.lists).toHaveLength(2);
  });

  it("drops a comparison when the host stops serving comparisons", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    test.watches[0]!.onRoute({ state: "unserved" });
    expect(test.snapshot().established).toBeNull();
    expect(test.snapshot().support.kind).toBe("unsupported");
    test.watches[0]!.onRoute(AVAILABLE);
    await settle();
    expect(test.lists).toHaveLength(2);
  });

  it("handles a route watch that answers before it returns", async () => {
    const owner = new WorkingCopyComparisonOwner({
      ...harnessDeps(),
      watchRoute: (_target, onRoute) => {
        onRoute(AVAILABLE);
        return { stop: () => {}, ask: () => {} };
      },
    });
    owner.register(SESSION, consumer("surface"));
    await settle();
    expect(owner.snapshot(SESSION).support.kind).toBe("available");
  });

  it("lists a ready comparison again when the session's files may be read again", async () => {
    const test = harness();
    test.owner.register(SESSION, consumer("count"));
    await settle();
    await answer(test.lists[0], listed("cmp_1"));
    const epoch = test.snapshot().accessEpoch;
    test.accessReady("conv_other");
    test.accessReady(SESSION);
    await settle();
    expect(test.lists).toHaveLength(2);
    expect(test.snapshot().accessEpoch).toBe(epoch + 1);
    // Still the same comparison on screen while it lists.
    expect(test.snapshot().established?.id).toBe("cmp_1");
  });

  it("asks an unreachable host again on focus, through the real route watcher", async () => {
    vi.useRealTimers();
    let reachable = false;
    const asks = vi.fn(async () => {
      if (!reachable) throw new Error("The host is offline.");
      return true;
    });
    setWorkspaceSidebarSourceForTests({
      ...DEFAULT_WORKSPACE_SIDEBAR_SOURCE,
      servesGitComparisons: asks,
    });
    const lists: unknown[] = [];
    const attention = new Set<() => void>();
    const owner = new WorkingCopyComparisonOwner({
      ...harnessDeps(),
      fetchListing: () => {
        lists.push(1);
        return new Promise<BranchPointListing>(() => {});
      },
      watchRoute: watchWorkspaceGitRoute,
      subscribeAttention: (listener) => {
        attention.add(listener);
        return () => attention.delete(listener);
      },
    });
    try {
      owner.register(SESSION, consumer("surface"));
      await vi.waitFor(() =>
        expect(owner.snapshot(SESSION).support).toEqual({
          kind: "unreachable",
          reason: "request_failed",
          message: "The host is offline.",
        }),
      );
      expect(lists).toHaveLength(0);
      reachable = true;
      for (const listener of attention) listener();
      await vi.waitFor(() => expect(owner.snapshot(SESSION).support.kind).toBe("available"));
      expect(asks).toHaveBeenCalledTimes(2);
      expect(lists).toHaveLength(1);
    } finally {
      owner.dispose();
      setWorkspaceSidebarSourceForTests(null);
    }
  });

  it("says a host that doesn't serve comparisons can't compare, and lists nothing", async () => {
    const test = harness({ route: { state: "unserved" } });
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    expect(test.snapshot().support).toMatchObject({ kind: "unsupported", reason: "not_served" });
    expect(test.lists).toHaveLength(0);
  });

  describe("recovery", () => {
    async function established() {
      const test = harness();
      test.owner.register(SESSION, consumer("surface"));
      await settle();
      await answer(
        test.lists[0],
        listed("cmp_1", [
          ["a.ts", "fa"],
          ["b.ts", "fb"],
        ]),
      );
      return test;
    }

    it("re-lists once per file, and the chain spans the replacement id", async () => {
      const test = await established();
      const first = test.owner.recover(SESSION, "cmp_1", "a.ts");
      await settle();
      await answer(test.lists[1], listed("cmp_2", [["a.ts", "fa"]]));
      await expect(first).resolves.toBe(true);
      expect(test.snapshot().established?.id).toBe("cmp_2");

      // The same file expiring under the new id is the reader's error.
      await expect(test.owner.recover(SESSION, "cmp_2", "a.ts")).resolves.toBe(false);
      expect(test.lists).toHaveLength(2);

      test.owner.allowRecovery(SESSION, "a.ts");
      const again = test.owner.recover(SESSION, "cmp_2", "a.ts");
      await settle();
      await answer(test.lists[2], listed("cmp_2", [["a.ts", "fa"]]));
      await expect(again).resolves.toBe(true);
    });

    it("keeps a file's spent chain when another file reads fine", async () => {
      const test = await established();
      const first = test.owner.recover(SESSION, "cmp_1", "a.ts");
      await settle();
      await answer(
        test.lists[1],
        listed("cmp_1", [
          ["a.ts", "fa"],
          ["b.ts", "fb"],
        ]),
      );
      await expect(first).resolves.toBe(true);
      test.owner.readSucceeded(SESSION, "cmp_1", "b.ts");
      await expect(test.owner.recover(SESSION, "cmp_1", "a.ts")).resolves.toBe(false);
      test.owner.readSucceeded(SESSION, "cmp_1", "a.ts");
      const reopened = test.owner.recover(SESSION, "cmp_1", "a.ts");
      await settle();
      await answer(test.lists[2], listed("cmp_1", [["a.ts", "fa"]]));
      await expect(reopened).resolves.toBe(true);
    });

    it("waits for a list that began after the refusal, under the same id", async () => {
      const test = await established();
      test.owner.invalidate(SESSION);
      await settle();
      const recovered = vi.fn();
      void test.owner.recover(SESSION, "cmp_1", "b.ts").then(recovered);
      // The list already in flight began before the refusal.
      await answer(test.lists[1], listed("cmp_1", [["b.ts", "fb"]]));
      expect(recovered).not.toHaveBeenCalled();
      await answer(test.lists[2], listed("cmp_1", [["b.ts", "fb"]]));
      expect(recovered).toHaveBeenCalledWith(true);
    });

    it("sends a reader of a replaced comparison to the new one without spending its chain", async () => {
      const test = await established();
      test.owner.invalidate(SESSION);
      await settle();
      await answer(test.lists[1], listed("cmp_2", [["a.ts", "fa"]]));
      await expect(test.owner.recover(SESSION, "cmp_1", "a.ts")).resolves.toBe(true);
      expect(test.lists).toHaveLength(2);
      const own = test.owner.recover(SESSION, "cmp_2", "a.ts");
      await settle();
      expect(test.lists).toHaveLength(3);
      await answer(test.lists[2], listed("cmp_2", [["a.ts", "fa"]]));
      await expect(own).resolves.toBe(true);
    });

    it("lets readers of the same file share one chain, without reopening it", async () => {
      const test = await established();
      const beforeRelist = test.owner.readToken(SESSION);
      const first = test.owner.recover(SESSION, "cmp_1", "a.ts", beforeRelist);
      // Another reader's refusal of the same read joins the chain in progress.
      expect(test.owner.recover(SESSION, "cmp_1", "a.ts", beforeRelist)).toBe(first);
      await settle();
      await answer(test.lists[1], listed("cmp_1", [["a.ts", "fa"]]));
      await expect(first).resolves.toBe(true);
      // A read that began before the chain's list gets its one retry; one after it doesn't.
      await expect(test.owner.recover(SESSION, "cmp_1", "a.ts", beforeRelist)).resolves.toBe(true);
      const afterRelist = test.owner.readToken(SESSION);
      await expect(test.owner.recover(SESSION, "cmp_1", "a.ts", afterRelist)).resolves.toBe(false);
      expect(test.lists).toHaveLength(2);
    });

    it("keeps a chain in progress when one reader succeeds during it", async () => {
      const test = await established();
      const pending = test.owner.recover(SESSION, "cmp_1", "a.ts", test.owner.readToken(SESSION));
      test.owner.readSucceeded(SESSION, "cmp_1", "a.ts");
      expect(test.owner.recover(SESSION, "cmp_1", "a.ts")).toBe(pending);
      await settle();
      await answer(test.lists[1], listed("cmp_1", [["a.ts", "fa"]]));
      await expect(pending).resolves.toBe(true);
    });

    it("answers waiting recoveries false when the owner resets", async () => {
      const test = await established();
      const pending = test.owner.recover(SESSION, "cmp_1", "a.ts");
      test.owner.reset();
      await expect(pending).resolves.toBe(false);
    });
  });
});

function harnessDeps() {
  return {
    fetchListing: () => new Promise<BranchPointListing>(() => {}),
    watchRoute: () => ({ stop: () => {}, ask: () => {} }),
    readIntent: () => "automatic" as const,
    subscribeIntent: () => () => {},
    now: () => Date.now(),
    setTimeout: (callback: () => void, ms: number) => setTimeout(callback, ms),
    clearTimeout: (handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    documentVisible: () => true,
    subscribeAttention: () => () => {},
  };
}

describe("a typed base and head", () => {
  const REVISIONS: WorkspaceGitRoute = { state: "served" };
  const V1: RevisionSpec = { base: "v1", head: "" };
  const typedListing = (id: string, spec: RevisionSpec): BranchPointListing => ({
    kind: "comparison",
    comparison: {
      ...comparison(id),
      revisions: {
        spec,
        baseDisplay: spec.base,
        headDisplay: spec.head,
        headIsWorkingCopy: !spec.head,
      },
    },
  });

  it("lists what was typed, even while the saved base is the last commit, and saves it", async () => {
    const test = harness({ route: REVISIONS });
    test.setIntent("head");
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    expect(test.lists).toHaveLength(0);
    test.owner.compareRevisions(SESSION, V1);
    await settle();
    expect(test.open(test.lists).map((list) => list.spec)).toEqual([V1]);
    expect(test.typed.get(typedKey(SESSION))).toEqual(V1);
    await answer(test.lists.at(-1), typedListing("cmp_v1", V1));
    expect([test.snapshot().custom, test.snapshot().established?.id]).toEqual([V1, "cmp_v1"]);
  });

  it("drops the previous comparison and any listing in flight when another is typed", async () => {
    const test = harness({ route: REVISIONS });
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists[0], listed("cmp_branch_point"));
    test.owner.compareRevisions(SESSION, V1);
    // Nothing from the branch point stays under the typed names, and its reads stop being current.
    expect(test.snapshot().established).toBeNull();
    expect(test.owner.isCurrent(SESSION, 0, "cmp_branch_point")).toBe(false);
    await settle();
    const first = test.lists.at(-1)!;
    test.owner.compareRevisions(SESSION, { base: "v2", head: "" });
    await settle();
    expect(first.signal.aborted).toBe(true);
    // The first listing answers late; it never becomes the session's comparison.
    first.resolve(typedListing("cmp_v1", V1));
    await settle();
    expect(test.snapshot().established).toBeNull();
    await answer(test.lists.at(-1), typedListing("cmp_v2", { base: "v2", head: "" }));
    expect(test.snapshot().established?.id).toBe("cmp_v2");
  });

  it("ends when a base is chosen, here or in another window", async () => {
    const test = harness({ route: REVISIONS });
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    test.owner.compareRevisions(SESSION, V1);
    test.owner.chose(SESSION);
    expect([test.snapshot().custom, test.typed.has(typedKey(SESSION))]).toEqual([null, false]);

    test.typedElsewhere(SESSION, V1);
    expect(test.snapshot().custom).toEqual(V1);
    test.typedElsewhere(SESSION, null);
    expect(test.snapshot().custom).toBeNull();

    // "Show uncommitted changes" shows them, rather than keeping the typed comparison.
    test.owner.compareRevisions(SESSION, V1);
    test.owner.showLastCommit(SESSION);
    expect([test.snapshot().custom, test.snapshot().headOverride]).toEqual([null, true]);
  });

  it("isn't listed where the host doesn't serve comparisons, and never falls back", async () => {
    const test = harness({ route: { state: "unserved" } });
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    test.owner.compareRevisions(SESSION, V1);
    await vi.advanceTimersByTimeAsync(CONNECT_FALLBACK_MS * 2);
    expect(test.lists).toHaveLength(0);
    const snapshot = test.snapshot();
    expect([snapshot.revisionSupport.kind, snapshot.support.kind]).toEqual([
      "unsupported",
      "unsupported",
    ]);
    expect(snapshot.connectTimedOut).toBe(false);
  });

  it("belongs to the session's workspace: a moved root drops what was listed and its typed comparison", async () => {
    const test = harness({ route: REVISIONS });
    const token = test.owner.register(SESSION, consumer("surface"));
    await settle();
    test.owner.compareRevisions(SESSION, V1);
    await settle();
    await answer(test.lists.at(-1), typedListing("cmp_v1", V1));
    expect(test.snapshot().established?.id).toBe("cmp_v1");

    expect(test.forgotten).toEqual([]);
    test.owner.update(SESSION, token, { ...consumer("surface"), root: "/other" });
    await settle();
    // Another repository: neither the typed names nor their listing carry over, nor its files.
    expect(test.forgotten).toEqual([SESSION]);
    expect([test.snapshot().custom, test.snapshot().established]).toEqual([null, null]);
    expect(test.owner.isCurrent(SESSION, 0, "cmp_v1")).toBe(false);
    expect(test.open(test.lists).at(-1)?.spec).toBeNull();

    // Back in the first workspace, its typed comparison applies again, listed afresh.
    test.owner.update(SESSION, token, consumer("surface"));
    await settle();
    expect(test.snapshot().custom).toEqual(V1);
    expect(test.snapshot().established).toBeNull();
  });

  it("settles the previous workspace's pending recovery as false when the root moves", async () => {
    const test = harness();
    const token = test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists.at(-1), listed("cmp_old", [["a.ts", "fa"]]));
    let recovered: boolean | null = null;
    void test.owner.recover(SESSION, "cmp_old", "a.ts").then((value) => {
      recovered = value;
    });
    await settle();
    test.owner.update(SESSION, token, { ...consumer("surface"), root: "/other" });
    await settle();
    expect(recovered).toBe(false);
  });

  it("gives a same-named file in the new workspace its own first recovery", async () => {
    const test = harness();
    const token = test.owner.register(SESSION, consumer("surface"));
    await settle();
    await answer(test.lists.at(-1), listed("cmp_old", [["a.ts", "fa"]]));
    const spent = test.owner.recover(SESSION, "cmp_old", "a.ts");
    await settle();
    await answer(test.lists.at(-1), listed("cmp_old_relisted", [["a.ts", "fa"]]));
    await expect(spent).resolves.toBe(true);
    test.owner.update(SESSION, token, { ...consumer("surface"), root: "/other" });
    await settle();
    await answer(test.lists.at(-1), listed("cmp_other", [["a.ts", "fb"]]));
    const before = test.lists.length;
    void test.owner.recover(SESSION, "cmp_other", "a.ts", test.owner.readToken(SESSION));
    await settle();
    expect(test.lists).toHaveLength(before + 1);
  });

  it("can't be typed before the session's workspace is known", async () => {
    const test = harness({ route: REVISIONS });
    test.owner.register(SESSION, { ...consumer("surface"), root: null });
    await settle();
    expect(test.snapshot().revisionSupport.kind).toBe("checking");
    test.owner.compareRevisions(SESSION, V1);
    expect([test.snapshot().custom, test.typed.size]).toEqual([null, 0]);
  });

  it("is read back for a session the owner sees for the first time, as after a reload", async () => {
    const test = harness({ route: REVISIONS });
    test.typed.set(typedKey(SESSION), V1);
    test.owner.register(SESSION, consumer("surface"));
    await settle();
    expect(test.open(test.lists).map((list) => list.spec)).toEqual([V1]);
  });
});
