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

const INDEX_KEY = "omnigent:pr-viewed:v1:index";
const DAY_MS = 24 * 60 * 60 * 1000;
const PR: PullRequestIdentity = {
  provider: "github",
  host: "github.com",
  repository: "acme/app",
  number: 7,
};

beforeEach(() => {
  localStorage.clear();
  identity.server = "https://omni.example";
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
    [
      "https://dev.azure.com/Org/Proj/_git/Repo/pullrequest/42?_a=files",
      "azure_devops",
      {
        provider: "azure_devops",
        host: "dev.azure.com",
        repository: "org/proj/_git/repo",
        number: 42,
      },
    ],
    [
      "https://org.visualstudio.com/Proj/_git/Repo/pullrequest/5",
      null,
      {
        provider: "azure_devops",
        host: "org.visualstudio.com",
        repository: "proj/_git/repo",
        number: 5,
      },
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

  it.each([
    ["not a url", null],
    ["ftp://github.com/acme/app/pull/7", null],
    ["https://github.com/acme/app/issues/7", null],
    ["https://github.com/acme/app/pull/seven", null],
    ["https://github.com/acme/app/pull/7", "gitlab"],
    ["https://github.com/acme/app/pull/7", "bitbucket"],
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

  it("reads a different head as empty and reports the stored head", () => {
    writeViewed(key, "head1", ["a.ts"]);
    expect(readViewed(key, "head2")).toEqual({ paths: new Set(), resetFromHead: "head1" });
    writeViewed(key, "head2", ["b.ts"]);
    expect(readViewed(key, "head2")).toEqual({ paths: new Set(["b.ts"]), resetFromHead: null });
  });

  it("reads corrupt or unknown values as empty", () => {
    localStorage.setItem(key, "{not json");
    expect(readViewed(key, "head1")).toEqual({ paths: new Set(), resetFromHead: null });
    localStorage.setItem(key, JSON.stringify({ v: 2, headSha: "head1", paths: ["a.ts"] }));
    expect(readViewed(key, "head1").paths.size).toBe(0);
  });

  it("prunes entries older than 90 days", () => {
    vi.useFakeTimers({ now: 100 * DAY_MS });
    const stale = viewedStorageKey("https://a", "alice", { ...PR, number: 1 });
    const recent = viewedStorageKey("https://a", "alice", { ...PR, number: 2 });
    writeViewed(stale, "h", ["a.ts"]);
    vi.setSystemTime(150 * DAY_MS);
    writeViewed(recent, "h", ["a.ts"]);
    vi.setSystemTime(191 * DAY_MS);
    writeViewed(key, "h", ["a.ts"]);

    expect(localStorage.getItem(stale)).toBeNull();
    expect(localStorage.getItem(recent)).not.toBeNull();
    expect(Object.keys(JSON.parse(localStorage.getItem(INDEX_KEY)!)).sort()).toEqual(
      [key, recent].sort(),
    );
  });

  it("keeps only the 500 most recently written entries", () => {
    vi.useFakeTimers({ now: DAY_MS });
    const keys = Array.from({ length: 501 }, (_, index) =>
      viewedStorageKey("https://a", "alice", { ...PR, number: index + 1 }),
    );
    for (const entryKey of keys) {
      writeViewed(entryKey, "h", ["a.ts"]);
      vi.advanceTimersByTime(1);
    }
    expect(localStorage.getItem(keys[0])).toBeNull();
    expect(localStorage.getItem(keys[1])).not.toBeNull();
    expect(Object.keys(JSON.parse(localStorage.getItem(INDEX_KEY)!))).toHaveLength(500);
  });

  it("keeps values in memory when storage throws", () => {
    const memoryKey = viewedStorageKey("https://a", "alice", { ...PR, number: 99 });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    expect(writeViewed(memoryKey, "head1", ["a.ts"])).toEqual({ persisted: false });
    expect(readViewed(memoryKey, "head1").paths).toEqual(new Set(["a.ts"]));

    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(readViewed(memoryKey, "head1").paths).toEqual(new Set(["a.ts"]));
  });
});

describe("useViewedFiles", () => {
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

  it("resets on a new head and reports it until the head changes again", () => {
    const { result, rerender } = renderHook(({ head }) => useViewedFiles(PR, head), {
      initialProps: { head: "head1" },
    });
    act(() => result.current.toggle("a.ts"));
    expect(result.current.resetNotice).toBe(false);

    rerender({ head: "head2" });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.resetNotice).toBe(true);
    act(() => result.current.toggle("b.ts"));
    expect(result.current.viewed).toEqual(new Set(["b.ts"]));
    expect(result.current.resetNotice).toBe(true);

    rerender({ head: "head2" });
    expect(result.current.resetNotice).toBe(true);
    expect(renderHook(() => useViewedFiles(PR, "head2")).result.current.resetNotice).toBe(false);
  });

  it("reports a storage failure and keeps marks in memory", () => {
    const { result } = renderHook(() => useViewedFiles({ ...PR, number: 77 }, "head1"));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    act(() => result.current.toggle("a.ts"));
    expect(result.current.persisted).toBe(false);
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
  });

  it("follows writes from another tab", () => {
    const { result } = renderHook(() => useViewedFiles(PR, "head1"));
    const key = viewedStorageKey("https://omni.example", "alice", PR);
    writeViewed(key, "head1", ["remote.ts"]);
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key }));
    });
    expect(result.current.viewed).toEqual(new Set(["remote.ts"]));

    localStorage.clear();
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: null }));
    });
    expect(result.current.viewed.size).toBe(0);
  });
});
