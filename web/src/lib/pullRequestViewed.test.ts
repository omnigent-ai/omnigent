import { act, renderHook } from "@testing-library/react";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  server: "https://omni.example" as string | null,
  user: "alice" as string | null,
}));
vi.mock("./host", () => ({ getOmnigentServerIdentity: () => identity.server }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => identity.user }));

import {
  patchFileHashes,
  unverifiedPatchHash,
  useViewedFiles,
  viewedStorageKey,
  writeViewed,
  type PullRequestIdentity,
  type ViewedMarks,
} from "./pullRequestViewed";

const KEY_PREFIX = "omnigent:pr-viewed:v2:";
const LEGACY_KEY_PREFIX = "omnigent:pr-viewed:v1:";
const DAY_MS = 24 * 60 * 60 * 1000;
const SERVER = "https://omni.example";
const PR: PullRequestIdentity = {
  provider: "github",
  host: "github.com",
  repository: "acme/app",
  number: 7,
};
const KEY = viewedStorageKey(SERVER, "alice", PR);

function storedKeys(): string[] {
  return Object.keys(localStorage).filter((key) => key.startsWith(KEY_PREFIX));
}

function failStorageWrites() {
  return vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("full", "QuotaExceededError");
  });
}

function stored(marks: ViewedMarks, updatedAt = Date.now()): string {
  return JSON.stringify({ v: 2, marks, updatedAt });
}

/** Deliver a `storage` event as another tab's change would. */
function storageEvent(key: string | null, storageArea: Storage = localStorage) {
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key, storageArea }));
  });
}

/** The parsed files of a patch, keyed by name, as the Changes tab renders them. */
function parsedFiles(patch: string): Map<string, FileDiffMetadata> {
  return new Map(
    parsePatchFiles(patch).flatMap((parsed) => parsed.files.map((file) => [file.name, file])),
  );
}

const hashesOf = (patch: string) => patchFileHashes(parsedFiles(patch));

const PATCH_A = [
  "diff --git a/a.ts b/a.ts",
  "index 1111111..2222222 100644",
  "--- a/a.ts",
  "+++ b/a.ts",
  "@@ -1,2 +1,2 @@",
  " keep",
  "-old",
  "+new",
  "",
].join("\n");
const PATCH_B = [
  "diff --git a/b.ts b/b.ts",
  "new file mode 100644",
  "index 0000000..3333333",
  "--- /dev/null",
  "+++ b/b.ts",
  "@@ -0,0 +1 @@",
  "+added",
  "",
].join("\n");

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

describe("viewedStorageKey", () => {
  it("isolates servers, users and pull requests, but not a missing provider or host and repository case", () => {
    const keys = new Set([
      viewedStorageKey("https://a", "alice", PR),
      viewedStorageKey("https://b", "alice", PR),
      viewedStorageKey("https://a", "bob", PR),
      viewedStorageKey("https://a", "alice", { ...PR, number: 70 }),
      viewedStorageKey("https://a", "alice", { ...PR, repository: "acme/other" }),
      viewedStorageKey("https://a", "alice", { ...PR, host: "ghe.example" }),
      viewedStorageKey("https://a", "alice", { ...PR, provider: "gitlab" }),
    ]);
    expect(keys.size).toBe(7);
    expect(viewedStorageKey(SERVER, "alice", { ...PR, provider: undefined })).toBe(KEY);
    expect(
      viewedStorageKey(SERVER, "alice", { ...PR, host: "GitHub.com", repository: "Acme/App" }),
    ).toBe(KEY);
  });
});

describe("patchFileHashes", () => {
  it("keeps a file's hash when other files change, and changes it when its own hunk changes", () => {
    const hashed = hashesOf(PATCH_A + PATCH_B);
    expect([...hashed.keys()]).toEqual(["a.ts", "b.ts"]);
    expect(hashesOf(PATCH_A.replace("+new", "+newer") + PATCH_B).get("a.ts")).not.toBe(
      hashed.get("a.ts"),
    );
    expect(hashesOf(PATCH_A + PATCH_B.replace("+added", "+added2")).get("a.ts")).toBe(
      hashed.get("a.ts"),
    );
  });

  it("changes when the hunk moves, but not for the index line", () => {
    expect(hashesOf(PATCH_A.replace("@@ -1,2 +1,2 @@", "@@ -5,2 +5,2 @@")).get("a.ts")).not.toBe(
      hashesOf(PATCH_A).get("a.ts"),
    );
    expect(hashesOf(PATCH_A.replace("1111111..2222222", "1111111..3333333")).get("a.ts")).toBe(
      hashesOf(PATCH_A).get("a.ts"),
    );
  });

  it("keeps a file apart from a later file whose path contains ' b/'", () => {
    // `z/my b/a.ts` contains " b/", so a greedy header match would file its hunks under `a.ts`.
    const colliding = [
      "diff --git a/z/my b/a.ts b/z/my b/a.ts",
      "index 3333333..4444444 100644",
      "--- a/z/my b/a.ts",
      "+++ b/z/my b/a.ts",
      "@@ -1 +1 @@",
      "-p",
      "+q",
      "",
    ].join("\n");
    const before = hashesOf(PATCH_A + colliding);
    expect(before.get("a.ts")).toBe(hashesOf(PATCH_A).get("a.ts"));
    expect(before.get("z/my b/a.ts")).toBe(hashesOf(colliding).get("z/my b/a.ts"));

    const edited = hashesOf(PATCH_A + colliding.replace("+q", "+r"));
    expect(edited.get("a.ts")).toBe(before.get("a.ts"));
    expect(edited.get("z/my b/a.ts")).not.toBe(before.get("z/my b/a.ts"));
  });

  it("does not let a quoted path's hunks change the file before it", () => {
    // The parser keeps git's octal escapes in the name, so this file never matches a list path.
    const quoted = [
      'diff --git "a/caf\\303\\251.ts" "b/caf\\303\\251.ts"',
      "index 1111111..2222222 100644",
      '--- "a/caf\\303\\251.ts"',
      '+++ "b/caf\\303\\251.ts"',
      "@@ -1 +1 @@",
      "-old",
      "+new",
      "",
    ].join("\n");
    const before = hashesOf(PATCH_A + quoted);
    expect(hashesOf(PATCH_A + quoted.replace("+new", "+newer")).get("a.ts")).toBe(
      before.get("a.ts"),
    );
  });

  it("hashes a hunk too large to pass as call arguments", () => {
    const lines = Array.from({ length: 150_000 }, (_, index) => `+line ${index}`);
    const patch = [
      "diff --git a/big.ts b/big.ts",
      "new file mode 100644",
      "index 0000000..1111111",
      "--- /dev/null",
      "+++ b/big.ts",
      `@@ -0,0 +1,${lines.length} @@`,
      ...lines,
      "",
    ].join("\n");
    expect(hashesOf(patch).has("big.ts")).toBe(true);
  });

  it("leaves out a file whose hunks were cut short, so its marks stay unverified", () => {
    // The parser accepts a hunk that ends early; its header counts show the lines are missing.
    const truncated = PATCH_A.replace("+new\n", "");
    expect(parsePatchFiles(truncated)[0].files[0].hunks).toHaveLength(1);
    expect(hashesOf(truncated).has("a.ts")).toBe(false);
  });

  it("leaves out a file without hunks, and is empty for a missing patch", () => {
    const binary = [
      "diff --git a/logo.png b/logo.png",
      "index 1111111..2222222 100644",
      "Binary files a/logo.png and b/logo.png differ",
      "",
    ].join("\n");
    expect(hashesOf(binary).has("logo.png")).toBe(false);
    expect(hashesOf("").size).toBe(0);
  });
});

describe("useViewedFiles", () => {
  const hashes = hashesOf(PATCH_A + PATCH_B);

  it("is unavailable until server, user, PR and head are known", () => {
    expect(renderHook(() => useViewedFiles(null, "head1", hashes)).result.current.available).toBe(
      false,
    );
    expect(renderHook(() => useViewedFiles(PR, null, hashes)).result.current.available).toBe(false);
    expect(renderHook(() => useViewedFiles(PR, "", hashes)).result.current.available).toBe(false);
    identity.user = null;
    expect(renderHook(() => useViewedFiles(PR, "head1", hashes)).result.current.available).toBe(
      false,
    );
    identity.user = "alice";
    identity.server = null;
    const { result } = renderHook(() => useViewedFiles(PR, "head1", hashes));
    expect(result.current.available).toBe(false);
    act(() => result.current.setMany(["a.ts"], true));
    expect(result.current.viewed.size).toBe(0);
    expect(storedKeys()).toEqual([]);
  });

  it("marks and unmarks files, keeps them across remounts, and isolates users", () => {
    const first = renderHook(() => useViewedFiles(PR, "head1", hashes));
    const second = renderHook(() => useViewedFiles(PR, "head1", hashes));
    act(() => first.result.current.setMany(["a.ts", "b.ts"], true));
    act(() => first.result.current.setMany(["b.ts", "missing.ts"], false));
    expect(second.result.current.viewed).toEqual(new Set(["a.ts"]));
    first.unmount();
    second.unmount();

    expect(renderHook(() => useViewedFiles(PR, "head1", hashes)).result.current.viewed).toEqual(
      new Set(["a.ts"]),
    );
    identity.user = "bob";
    expect(renderHook(() => useViewedFiles(PR, "head1", hashes)).result.current.viewed.size).toBe(
      0,
    );
  });

  it("keeps an unchanged file's mark on a new head and flags a changed one", () => {
    const { result, rerender } = renderHook(
      ({ head, patchHashes }) => useViewedFiles(PR, head, patchHashes),
      { initialProps: { head: "head1", patchHashes: hashes } },
    );
    act(() => result.current.setMany(["a.ts", "b.ts"], true));

    const changedB = hashesOf(PATCH_A + PATCH_B.replace("+added", "+changed"));
    rerender({ head: "head2", patchHashes: changedB });
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
    expect(result.current.changed).toEqual(new Set(["b.ts"]));

    act(() => result.current.setMany(["b.ts"], true));
    expect(result.current.changed.size).toBe(0);
    expect(result.current.viewed).toEqual(new Set(["a.ts", "b.ts"]));
  });

  it("keeps the newest head viewed when an older head's tab marks the same file", () => {
    const newerHashes = hashesOf(PATCH_A.replace("+new", "+newer"));
    const newer = renderHook(() => useViewedFiles(PR, "head2", newerHashes));
    act(() => newer.result.current.setMany(["a.ts"], true));

    // The older tab is already mounted when another tab writes b.ts, without a storage event here.
    const older = renderHook(() => useViewedFiles(PR, "head1", hashes));
    localStorage.setItem(KEY, stored({ ...readMarksFromStorage(), "b.ts": [hashes.get("b.ts")!] }));
    act(() => older.result.current.setMany(["a.ts"], true));

    expect(newer.result.current.viewed).toEqual(new Set(["a.ts"]));
    expect(newer.result.current.changed.size).toBe(0);
    expect(older.result.current.viewed).toEqual(new Set(["a.ts", "b.ts"]));
    expect(readMarksFromStorage()).toEqual({
      "a.ts": [newerHashes.get("a.ts"), hashes.get("a.ts")],
      "b.ts": [hashes.get("b.ts")],
    });
  });

  it("keeps the three newest hashes of a file, and unmarking clears all of them", () => {
    const aAt = (line: string) => hashesOf(PATCH_A.replace("+new", `+${line}`));
    const { result, rerender } = renderHook(
      ({ head, patchHashes }) => useViewedFiles(PR, head, patchHashes),
      { initialProps: { head: "h1", patchHashes: aAt("one") } },
    );
    for (const [head, line] of [
      ["h1", "one"],
      ["h2", "two"],
      ["h3", "three"],
      ["h4", "four"],
    ] as const) {
      rerender({ head, patchHashes: aAt(line) });
      act(() => result.current.setMany(["a.ts"], true));
    }
    expect(JSON.parse(localStorage.getItem(KEY)!).marks["a.ts"]).toHaveLength(3);

    // Each retained version still reads as viewed. The dropped first version reads as changed.
    for (const [head, line] of [
      ["h2", "two"],
      ["h3", "three"],
      ["h4", "four"],
    ] as const) {
      rerender({ head, patchHashes: aAt(line) });
      expect(result.current.viewed).toEqual(new Set(["a.ts"]));
    }
    rerender({ head: "h1", patchHashes: aAt("one") });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.changed).toEqual(new Set(["a.ts"]));

    // Unmarking a retained version forgets every version, so none reads as changed afterwards.
    rerender({ head: "h3", patchHashes: aAt("three") });
    act(() => result.current.setMany(["a.ts"], false));
    expect(readMarksFromStorage()).toEqual({});
    for (const [head, line] of [
      ["h1", "one"],
      ["h2", "two"],
      ["h4", "four"],
    ] as const) {
      rerender({ head, patchHashes: aAt(line) });
      expect(result.current.changed.size).toBe(0);
    }

    // A view still shows the file viewed at h2 while another tab has replaced the history without
    // that hash. The unmark, sent before that view hears of the change, still clears every hash.
    rerender({ head: "h2", patchHashes: aAt("two") });
    act(() => result.current.setMany(["a.ts"], true));
    localStorage.setItem(
      KEY,
      stored({ "a.ts": [aAt("three").get("a.ts")!, aAt("four").get("a.ts")!] }),
    );
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
    act(() => result.current.setMany(["a.ts"], false));
    expect(readMarksFromStorage()).toEqual({});
  });

  it("stays silent for history recorded only without comparable hunks", () => {
    const { result, rerender } = renderHook(
      ({ head, patchHashes }) => useViewedFiles(PR, head, patchHashes),
      { initialProps: { head: "head1", patchHashes: new Map<string, string>() } },
    );
    act(() => result.current.setMany(["a.ts"], true));
    expect(readMarksFromStorage()).toEqual({ "a.ts": [unverifiedPatchHash("head1")] });

    // Missing from the patch on the new head: silently not viewed, with no changed flag.
    rerender({ head: "head2", patchHashes: new Map() });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.changed.size).toBe(0);

    // Comparable hunks appear, but the only history is a stand-in: still silent.
    rerender({ head: "head2", patchHashes: new Map(hashes) });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.changed.size).toBe(0);

    rerender({ head: "head1", patchHashes: new Map() });
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
  });

  it("reports a change when the file's history includes a comparable hash", () => {
    const { result, rerender } = renderHook(
      ({ head, patchHashes }) => useViewedFiles(PR, head, patchHashes),
      { initialProps: { head: "head1", patchHashes: new Map<string, string>() } },
    );
    act(() => result.current.setMany(["a.ts"], true));

    // Marked again once its hunks are comparable on head2: the history mixes both kinds.
    rerender({ head: "head2", patchHashes: new Map(hashes) });
    act(() => result.current.setMany(["a.ts"], true));
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));

    rerender({
      head: "head3",
      patchHashes: new Map(hashesOf(PATCH_A.replace("+new", "+newer") + PATCH_B)),
    });
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.changed).toEqual(new Set(["a.ts"]));
  });

  it("keeps a comparable mark when later heads only record stand-ins", () => {
    const { result, rerender } = renderHook(
      ({ head, patchHashes }) => useViewedFiles(PR, head, patchHashes),
      { initialProps: { head: "head1", patchHashes: hashes } },
    );
    act(() => result.current.setMany(["a.ts"], true));
    for (const head of ["head2", "head3", "head4"]) {
      rerender({ head, patchHashes: new Map() });
      act(() => result.current.setMany(["a.ts"], true));
    }
    expect(readMarksFromStorage()["a.ts"]).toContain(hashes.get("a.ts"));

    rerender({ head: "head1", patchHashes: hashes });
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
  });

  it("keeps marks in memory when storage fails and reports them unsaved until a write succeeds", () => {
    const writes = failStorageWrites();
    const first = renderHook(({ pr }) => useViewedFiles(pr, "head1", hashes), {
      initialProps: { pr: PR },
    });
    act(() => first.result.current.setMany(["a.ts"], true));
    expect(first.result.current.persisted).toBe(false);
    expect(first.result.current.viewed).toEqual(new Set(["a.ts"]));

    // Another pull request has its own key, so it is not unsaved; coming back shows the marks again.
    first.rerender({ pr: { ...PR, number: 8 } });
    expect(first.result.current.persisted).toBe(true);
    expect(first.result.current.viewed.size).toBe(0);
    first.rerender({ pr: PR });
    expect(first.result.current.persisted).toBe(false);
    expect(first.result.current.viewed).toEqual(new Set(["a.ts"]));
    first.unmount();

    // A denied read still shows the marks held in memory.
    const reads = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const remounted = renderHook(() => useViewedFiles(PR, "head1", hashes));
    expect(remounted.result.current.viewed).toEqual(new Set(["a.ts"]));
    expect(remounted.result.current.persisted).toBe(false);
    reads.mockRestore();

    // Another tab persists c.ts while this tab's writes are refused, and no event reaches here yet.
    writes.mockRestore();
    localStorage.setItem(KEY, stored({ "c.ts": ["c1"] }));
    act(() => remounted.result.current.setMany(["b.ts"], true));
    expect(remounted.result.current.persisted).toBe(true);
    expect(readMarksFromStorage()).toEqual({
      "a.ts": [hashes.get("a.ts")],
      "b.ts": [hashes.get("b.ts")],
      "c.ts": ["c1"],
    });
  });

  it("applies another tab's writes under unsaved marks, and a clear discards them", () => {
    const { result } = renderHook(() => useViewedFiles(PR, "head1", hashes));
    localStorage.setItem(KEY, stored({ "a.ts": [hashes.get("a.ts")!] }));
    storageEvent(KEY);
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));

    const writes = failStorageWrites();
    act(() => result.current.setMany(["b.ts"], true));
    expect(result.current.persisted).toBe(false);
    writes.mockRestore();

    // The unsaved b.ts stays on top of the other tab's write, which replaces the stored marks.
    localStorage.setItem(KEY, stored({ "c.ts": [unverifiedPatchHash("head1")] }));
    storageEvent(KEY);
    expect(result.current.viewed).toEqual(new Set(["b.ts", "c.ts"]));
    expect(result.current.persisted).toBe(false);

    localStorage.clear();
    storageEvent(null);
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.persisted).toBe(true);
  });

  it("ignores sessionStorage events", () => {
    const { result } = renderHook(() => useViewedFiles(PR, "head1", hashes));
    failStorageWrites();
    act(() => result.current.setMany(["a.ts"], true));

    storageEvent(null, sessionStorage);
    storageEvent(KEY, sessionStorage);
    expect(result.current.viewed).toEqual(new Set(["a.ts"]));
    expect(result.current.persisted).toBe(false);
  });

  it.each([
    ["malformed JSON", "{not json"],
    ["an unsupported version", JSON.stringify({ v: 1, paths: ["a.ts"], updatedAt: 1 })],
    ["a non-array hash list", JSON.stringify({ v: 2, marks: { "a.ts": "a1" }, updatedAt: 1 })],
    ["a non-string hash", JSON.stringify({ v: 2, marks: { "a.ts": [1] }, updatedAt: 1 })],
    ["a missing timestamp", JSON.stringify({ v: 2, marks: { "a.ts": ["a1"] } })],
    [
      "a non-numeric timestamp",
      JSON.stringify({ v: 2, marks: { "a.ts": ["a1"] }, updatedAt: "1" }),
    ],
  ])("reads %s as no marks", (_label, value) => {
    const staleHashes = new Map([["a.ts", "a1"]]);
    localStorage.setItem(KEY, value);
    const { result } = renderHook(() => useViewedFiles(PR, "head1", staleHashes));
    expect(result.current.viewed.size).toBe(0);
    expect(result.current.changed.size).toBe(0);
  });
});

describe("storage retention", () => {
  it("removes version 1 entries and entries older than 90 days, and leaves other keys alone", () => {
    vi.useFakeTimers({ now: 100 * DAY_MS });
    const stale = viewedStorageKey(SERVER, "alice", { ...PR, number: 2 });
    const recent = viewedStorageKey(SERVER, "alice", { ...PR, number: 3 });
    writeViewed(stale, { "a.ts": ["h"] });
    vi.setSystemTime(150 * DAY_MS);
    writeViewed(recent, { "a.ts": ["h"] });
    localStorage.setItem(
      `${LEGACY_KEY_PREFIX}${SERVER}|head1`,
      JSON.stringify({ v: 1, paths: ["a.ts"], updatedAt: 1 }),
    );
    localStorage.setItem("omnigent:unrelated", "keep");
    localStorage.setItem(`${KEY_PREFIX}corrupt`, "{");
    vi.setSystemTime(191 * DAY_MS);
    writeViewed(KEY, { "a.ts": ["h"] });

    expect(storedKeys().sort()).toEqual([KEY, recent].sort());
    expect(Object.keys(localStorage).filter((key) => key.startsWith(LEGACY_KEY_PREFIX))).toEqual(
      [],
    );
    expect(localStorage.getItem("omnigent:unrelated")).toBe("keep");
  });

  it("keeps only the 500 most recently written entries", () => {
    vi.useFakeTimers({ now: DAY_MS });
    const keys = Array.from({ length: 501 }, (_, index) =>
      viewedStorageKey(SERVER, "alice", { ...PR, number: index + 1 }),
    );
    for (const entryKey of keys.slice(0, 500)) {
      writeViewed(entryKey, { "a.ts": ["h"] });
      vi.advanceTimersByTime(1);
    }
    // Rewriting the oldest entry makes the second-oldest the one to evict.
    writeViewed(keys[0], { "b.ts": ["h"] });
    vi.advanceTimersByTime(1);
    writeViewed(keys[500], { "a.ts": ["h"] });

    expect(localStorage.getItem(keys[0])).not.toBeNull();
    expect(localStorage.getItem(keys[1])).toBeNull();
    expect(storedKeys()).toHaveLength(500);
  });

  it("keeps the stored marks of the entry being written when its write fails", () => {
    vi.useFakeTimers({ now: DAY_MS });
    writeViewed(KEY, { "a.ts": ["old"] });
    vi.setSystemTime(200 * DAY_MS);
    failStorageWrites();
    writeViewed(KEY, { "a.ts": ["new"] });
    vi.restoreAllMocks();

    expect(readMarksFromStorage()).toEqual({ "a.ts": ["old"] });
  });
});

/** The marks stored under this PR's key, bypassing the in-memory fallback. */
function readMarksFromStorage(): ViewedMarks {
  return JSON.parse(localStorage.getItem(KEY)!).marks;
}
