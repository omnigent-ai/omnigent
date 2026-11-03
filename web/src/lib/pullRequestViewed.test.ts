import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  server: "https://omni.example" as string | null,
  user: "alice" as string | null,
}));
vi.mock("./host", () => ({ getOmnigentServerIdentity: () => identity.server }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => identity.user }));

import {
  pullRequestIdentityFromUrl,
  readViewed,
  useViewedFiles,
  viewedStorageKey,
  writeViewed,
  type PullRequestIdentity,
} from "./pullRequestViewed";

const KEY_PREFIX = "omnigent:pr-viewed:v1:";
const DAY_MS = 24 * 60 * 60 * 1000;
const SERVER = "https://omni.example";
const PR: PullRequestIdentity = {
  provider: "github",
  host: "github.com",
  repository: "acme/app",
  number: 7,
};
const AZURE_PR: PullRequestIdentity = {
  provider: "azure_devops",
  host: "dev.azure.com",
  repository: "org/proj/repo",
  number: 42,
};

function storedKeys(): string[] {
  return Object.keys(localStorage).filter((key) => key.startsWith(KEY_PREFIX));
}

function failStorageWrites() {
  return vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("full", "QuotaExceededError");
  });
}

/** Simulate another tab: write storage directly, then deliver its `storage` event. */
function remoteWrite(key: string, value: string | null) {
  if (value === null) localStorage.clear();
  else localStorage.setItem(key, value);
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key: value === null ? null : key }));
  });
}

beforeEach(() => {
  localStorage.clear();
  // A cleared-storage event also drops the module's in-memory fallback values.
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  identity.server = SERVER;
  identity.user = "alice";
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("pullRequestIdentityFromUrl", () => {
  it.each([
    [
      "https://github.com/Acme/App/pull/7",
      "github",
      { provider: "github", host: "github.com", repository: "acme/app", number: 7 },
    ],
    [
      "https://GHE.corp.example/team/svc/pull/12/files?w=1#diff",
      null,
      { provider: "github", host: "ghe.corp.example", repository: "team/svc", number: 12 },
    ],
    [
      "https://gitlab.com/Group/Sub/Project/-/merge_requests/3/diffs",
      "gitlab",
      { provider: "gitlab", host: "gitlab.com", repository: "group/sub/project", number: 3 },
    ],
    ["https://dev.azure.com/Org/Proj/_git/Repo/pullrequest/42?_a=files", "azure_devops", AZURE_PR],
    [
      "https://dev.azure.com/Org/Repo/_git/Repo/pullrequest/5",
      null,
      { ...AZURE_PR, repository: "org/repo/repo", number: 5 },
    ],
  ])("parses %s", (url, provider, expected) => {
    expect(pullRequestIdentityFromUrl(url, provider)).toEqual(expected);
  });

  it("maps URL variants of one PR to the same identity", () => {
    const variants = [
      "https://github.com/acme/app/pull/7",
      "https://GitHub.com/ACME/app/pull/7/",
      "https://github.com/acme/app/pull/7/files",
      "https://github.com/acme/app/pull/7#issuecomment-1",
    ].map((url) => pullRequestIdentityFromUrl(url, "github"));
    for (const variant of variants) expect(variant).toEqual(PR);
  });

  it("maps Azure DevOps aliases to the dev.azure.com identity", () => {
    const variants = [
      "https://dev.azure.com/org/proj/_git/repo/pullrequest/42",
      "https://org.visualstudio.com/Proj/_git/Repo/pullrequest/42",
      "https://org.visualstudio.com/DefaultCollection/Proj/_git/Repo/pullRequest/42/",
    ].map((url) => pullRequestIdentityFromUrl(url, null));
    for (const variant of variants) expect(variant).toEqual(AZURE_PR);
    expect(
      pullRequestIdentityFromUrl("https://dev.azure.com/org/_git/repo/pullrequest/1", null),
    ).toEqual({ ...AZURE_PR, repository: "org/repo/repo", number: 1 });
  });

  it.each([
    ["not a url", null],
    ["ftp://github.com/acme/app/pull/7", null],
    ["https://github.com/acme/app/issues/7", null],
    ["https://github.com/acme/app/pull/seven", null],
    ["https://github.com/acme/app/pull/7", "gitlab"],
    ["https://github.com/acme/app/pull/7", "bitbucket"],
    ["https://github.com/acme/app/pull/7", "constructor"],
    ["https://example.org/org/proj/_git/repo/pullrequest/7", null],
    ["https://example.org/org/proj/_git/repo/pullrequest/7", "azure_devops"],
    ["https://dev.azure.com:8443/org/proj/_git/repo/pullrequest/7", null],
    ["https://dev.azure.com/org/a/b/_git/repo/pullrequest/7", null],
    ["https://dev.azure.com/_git/repo/pullrequest/7", null],
  ])("rejects %s for provider %s", (url, provider) => {
    expect(pullRequestIdentityFromUrl(url, provider)).toBeNull();
  });
});

describe("viewedStorageKey", () => {
  it("isolates servers, users and pull requests", () => {
    const keys = new Set([
      viewedStorageKey("https://a", "alice", PR),
      viewedStorageKey("https://b", "alice", PR),
      viewedStorageKey("https://a", "bob", PR),
      viewedStorageKey("https://a", "alice", { ...PR, number: 8 }),
      viewedStorageKey("https://a", "alice", { ...PR, repository: "acme/other" }),
      viewedStorageKey("https://a", "alice", { ...PR, provider: "gitlab" }),
    ]);
    expect(keys.size).toBe(6);
    expect(viewedStorageKey("https://a", "alice", { ...PR, host: "GitHub.com" })).toBe(
      viewedStorageKey("https://a", "alice", PR),
    );
  });
});

describe("readViewed / writeViewed", () => {
  const key = viewedStorageKey("https://a", "alice", PR);

  it("round-trips the paths for the same head", () => {
    expect(writeViewed(key, "head1", ["a.ts", "b.ts", "a.ts"])).toEqual({ persisted: true });
    expect(readViewed(key, "head1")).toEqual({
      paths: new Set(["a.ts", "b.ts"]),
      resetFromHead: null,
    });
  });

  it("reads a new head as empty and reports the latest other head", () => {
    vi.useFakeTimers({ now: DAY_MS });
    writeViewed(key, "head1", ["a.ts"]);
    vi.advanceTimersByTime(1);
    writeViewed(key, "head2", ["b.ts"]);
    expect(readViewed(key, "head3")).toEqual({ paths: new Set(), resetFromHead: "head2" });
  });

  it("does not let a write for an older head erase a newer head's marks", () => {
    writeViewed(key, "head2", ["new.ts"]);
    writeViewed(key, "head1", ["old.ts"]);
    expect(readViewed(key, "head2").paths).toEqual(new Set(["new.ts"]));
    expect(readViewed(key, "head1").paths).toEqual(new Set(["old.ts"]));
  });

  it("keeps the three most recently written heads", () => {
    vi.useFakeTimers({ now: DAY_MS });
    for (const head of ["head1", "head2", "head3", "head4"]) {
      writeViewed(key, head, [`${head}.ts`]);
      vi.advanceTimersByTime(1);
    }
    expect(readViewed(key, "head1").paths.size).toBe(0);
    expect(readViewed(key, "head2").paths).toEqual(new Set(["head2.ts"]));
    expect(readViewed(key, "head4").paths).toEqual(new Set(["head4.ts"]));
  });

  it("reads corrupt or unknown values as empty", () => {
    localStorage.setItem(key, "{not json");
    expect(readViewed(key, "head1")).toEqual({ paths: new Set(), resetFromHead: null });
    localStorage.setItem(key, JSON.stringify({ v: 1, headSha: "head1", paths: ["a.ts"] }));
    expect(readViewed(key, "head1").paths.size).toBe(0);
  });

  it("prunes entries older than 90 days", () => {
    vi.useFakeTimers({ now: 100 * DAY_MS });
    const stale = viewedStorageKey("https://a", "alice", { ...PR, number: 1 });
    const recent = viewedStorageKey("https://a", "alice", { ...PR, number: 2 });
    writeViewed(stale, "h", ["a.ts"]);
    vi.setSystemTime(150 * DAY_MS);
    writeViewed(recent, "h", ["a.ts"]);
    localStorage.setItem(`${KEY_PREFIX}corrupt`, "{");
    vi.setSystemTime(191 * DAY_MS);
    writeViewed(key, "h", ["a.ts"]);

    expect(storedKeys().sort()).toEqual([key, recent].sort());
  });

  it("keeps only the 500 most recently written entries", () => {
    vi.useFakeTimers({ now: DAY_MS });
    const keys = Array.from({ length: 501 }, (_, index) =>
      viewedStorageKey("https://a", "alice", { ...PR, number: index + 1 }),
    );
    for (const entryKey of keys.slice(0, 500)) {
      writeViewed(entryKey, "h", ["a.ts"]);
      vi.advanceTimersByTime(1);
    }
    // Rewriting the oldest entry makes the second-oldest the one to evict.
    writeViewed(keys[0], "h", ["b.ts"]);
    vi.advanceTimersByTime(1);
    writeViewed(keys[500], "h", ["a.ts"]);

    expect(localStorage.getItem(keys[0])).not.toBeNull();
    expect(localStorage.getItem(keys[1])).toBeNull();
    expect(storedKeys()).toHaveLength(500);
  });

  it("keeps values in memory when storage throws, until a write succeeds", () => {
    const writes = failStorageWrites();
    expect(writeViewed(key, "head1", ["a.ts"])).toEqual({ persisted: false });
    expect(readViewed(key, "head1").paths).toEqual(new Set(["a.ts"]));

    const reads = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const otherKey = viewedStorageKey("https://a", "alice", { ...PR, number: 98 });
    expect(readViewed(key, "head1").paths).toEqual(new Set(["a.ts"]));
    expect(readViewed(otherKey, "head1")).toEqual({ paths: new Set(), resetFromHead: null });

    writes.mockRestore();
    reads.mockRestore();
    expect(writeViewed(key, "head1", ["a.ts", "b.ts"])).toEqual({ persisted: true });
    expect(JSON.parse(localStorage.getItem(key)!).heads.head1.paths).toEqual(["a.ts", "b.ts"]);
  });
});

describe("useViewedFiles", () => {
  const key = viewedStorageKey(SERVER, "alice", PR);

  it("is unavailable until server, user, PR and head are known", () => {
    expect(renderHook(() => useViewedFiles(null, "head1")).result.current.available).toBe(false);
    expect(renderHook(() => useViewedFiles(PR, null)).result.current.available).toBe(false);
    identity.user = null;
    expect(renderHook(() => useViewedFiles(PR, "head1")).result.current.available).toBe(false);
    identity.user = "alice";
    identity.server = null;
    const { result } = renderHook(() => useViewedFiles(PR, "head1"));
    expect(result.current.available).toBe(false);
    act(() => result.current.toggle("a.ts"));
    expect(result.current.viewed.size).toBe(0);
    expect(storedKeys()).toEqual([]);
  });

  it("persists toggles across remounts and isolates users", () => {
    const first = renderHook(() => useViewedFiles(PR, "head1"));
    expect(first.result.current.available).toBe(true);
    act(() => first.result.current.toggle("a.ts"));
    act(() => first.result.current.setMany(["b.ts", "c.ts"], true));
    act(() => first.result.current.toggle("c.ts"));
    expect(first.result.current.viewed).toEqual(new Set(["a.ts", "b.ts"]));
    first.unmount();

    expect(renderHook(() => useViewedFiles(PR, "head1")).result.current.viewed).toEqual(
      new Set(["a.ts", "b.ts"]),
    );
    identity.user = "bob";
    expect(renderHook(() => useViewedFiles(PR, "head1")).result.current.viewed.size).toBe(0);
  });

  it("keeps simultaneous consumers in one tab in sync", () => {
    const first = renderHook(() => useViewedFiles(PR, "head1"));
    const second = renderHook(() => useViewedFiles(PR, "head1"));
    act(() => first.result.current.toggle("a.ts"));
    expect(second.result.current.viewed).toEqual(new Set(["a.ts"]));
    act(() => second.result.current.toggle("a.ts"));
    expect(first.result.current.viewed.size).toBe(0);
  });

  it("starts a new head empty and reports the reset until that head gets a mark", () => {
    const { result, rerender } = renderHook(({ head }) => useViewedFiles(PR, head), {
      initialProps: { head: "head1" },
    });
    act(() => result.current.toggle("a.ts"));
    expect(result.current.resetNotice).toBe(false);

    rerender({ head: "head2" });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.resetNotice).toBe(true);
    expect(renderHook(() => useViewedFiles(PR, "head2")).result.current.resetNotice).toBe(true);

    act(() => result.current.toggle("b.ts"));
    expect(result.current.viewed).toEqual(new Set(["b.ts"]));
    expect(result.current.resetNotice).toBe(false);

    rerender({ head: "head1" });
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
  });

  it("reports unsaved marks from the store, across remounts and identity changes", () => {
    failStorageWrites();
    const first = renderHook(({ pr }) => useViewedFiles(pr, "head1"), {
      initialProps: { pr: PR },
    });
    act(() => first.result.current.toggle("a.ts"));
    expect(first.result.current.persisted).toBe(false);
    expect(first.result.current.viewed).toEqual(new Set(["a.ts"]));
    first.rerender({ pr: { ...PR, number: 8 } });
    expect(first.result.current.persisted).toBe(true);
    first.unmount();

    const remounted = renderHook(() => useViewedFiles(PR, "head1"));
    expect(remounted.result.current.persisted).toBe(false);
    expect(remounted.result.current.viewed).toEqual(new Set(["a.ts"]));
  });

  it("follows writes and clears from another tab, even over unsaved marks", () => {
    const { result } = renderHook(() => useViewedFiles(PR, "head1"));
    const remote = JSON.stringify({
      v: 1,
      heads: { head1: { paths: ["remote.ts"], updatedAt: Date.now() } },
    });
    remoteWrite(key, remote);
    expect(result.current.viewed).toEqual(new Set(["remote.ts"]));

    const writes = failStorageWrites();
    act(() => result.current.toggle("local.ts"));
    expect(result.current.viewed).toEqual(new Set(["remote.ts", "local.ts"]));
    writes.mockRestore();
    remoteWrite(key, remote);
    expect(result.current.viewed).toEqual(new Set(["remote.ts"]));
    expect(result.current.persisted).toBe(true);

    failStorageWrites();
    act(() => result.current.toggle("local.ts"));
    vi.restoreAllMocks();
    remoteWrite(key, null);
    expect(result.current.viewed.size).toBe(0);
    act(() => result.current.toggle("next.ts"));
    expect(readViewed(key, "head1").paths).toEqual(new Set(["next.ts"]));
  });
});
