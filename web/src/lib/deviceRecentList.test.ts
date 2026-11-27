// A device recent list puts what was just recorded first, the last of a batch first as the most
// recent, and moves an item recorded again to the front instead of keeping it twice.

import { renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { z } from "zod";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const identity = vi.hoisted(() => ({
  server: "https://server.example.com",
  base: "/api",
  user: "user-1" as string | null,
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.user,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => identity.server,
}));
vi.mock("@/lib/basePath", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getApiBasePath: () => identity.base,
}));

import { createDeviceRecentList } from "./deviceRecentList";

const list = createDeviceRecentList({
  storagePrefix: "omnigent:test-recent",
  field: "items",
  maxItems: 3,
  maxScopes: 2,
  item: z.string(),
  same: (left, right) => left === right,
});
const SCOPE = ["github.com", "example-org/example-repo"];

afterEach(() => {
  list.resetForTests();
  identity.server = "https://server.example.com";
  identity.base = "/api";
  identity.user = "user-1";
  identity.listeners.clear();
  window.localStorage.clear();
});

/** The workspace user becomes `user`, and identity tells its subscribers. */
function identityBecomes(user: string | null) {
  identity.user = user;
  for (const listener of [...identity.listeners]) listener();
}

it("puts a batch first, its last item first, and keeps the list to its limit", () => {
  list.record(SCOPE, ["a"], list.owner());
  list.record(SCOPE, ["b", "c"], list.owner());
  expect(list.read(SCOPE)).toEqual(["c", "b", "a"]);
  list.record(SCOPE, ["a", "d"], list.owner());
  expect(list.read(SCOPE)).toEqual(["d", "a", "c"]);
  // A batch naming an item twice keeps it once, where it was recorded last.
  list.record(SCOPE, ["b", "e", "b"], list.owner());
  expect(list.read(SCOPE)).toEqual(["b", "e", "d"]);
});

it("keeps the saved lists through an unresolved identity, for the same user signing in", () => {
  list.record(SCOPE, ["a"], list.owner());
  const stop = list.subscribe(() => {});
  // A page loads before identity resolves, or its first request fails.
  identityBecomes(null);
  expect(list.hasSaved()).toBe(true);
  identityBecomes("user-1");
  expect(list.read(SCOPE)).toEqual(["a"]);
  stop();
});

it("drops the saved lists once a different user is confirmed, even after an unresolved identity", () => {
  list.record(SCOPE, ["a"], list.owner());
  const stop = list.subscribe(() => {});
  identityBecomes(null);
  identityBecomes("user-2");
  expect(list.hasSaved()).toBe(false);
  identityBecomes("user-1");
  expect(list.read(SCOPE)).toEqual([]);
  stop();
});

it("keeps the first items a direct write names past the limit, and never loses the list", () => {
  list.write(SCOPE, ["a"]);
  list.write(SCOPE, ["b", "c", "d", "e"]);
  expect(list.read(SCOPE)).toEqual(["b", "c", "d"]);
});

it("keeps the newest scopes, and on a tie the one just written", () => {
  vi.spyOn(Date, "now").mockReturnValue(1_000);
  try {
    list.record(["a"], ["x"], list.owner());
    list.record(["b"], ["x"], list.owner());
    list.record(["c"], ["x"], list.owner());
    expect(list.read(["c"])).toEqual(["x"]);
    expect([list.read(["a"]), list.read(["b"])].filter((items) => items.length > 0)).toHaveLength(
      1,
    );
  } finally {
    vi.mocked(Date.now).mockRestore();
  }
});

it("reads a list saved under a higher limit as its first items", () => {
  const roomier = createDeviceRecentList({
    storagePrefix: "omnigent:test-recent",
    field: "items",
    maxItems: 5,
    maxScopes: 2,
    item: z.string(),
    same: (left, right) => left === right,
  });
  roomier.write(SCOPE, ["a", "b", "c", "d", "e"]);
  list.prune();
  expect(list.read(SCOPE)).toEqual(["a", "b", "c"]);
  expect(roomier.read(SCOPE)).toEqual(["a", "b", "c", "d", "e"]);
});

it("leaves another kind's lists alone when its prefix starts with this one's", () => {
  const branches = createDeviceRecentList({
    storagePrefix: "omnigent:test-recent:branches",
    field: "branches",
    maxItems: 3,
    maxScopes: 2,
    item: z.number(),
    same: (left, right) => left === right,
  });
  branches.record(SCOPE, [7], branches.owner());
  list.record(SCOPE, ["a"], list.owner());
  list.prune();
  expect(branches.read(SCOPE)).toEqual([7]);
  branches.resetForTests();
});

it("keeps every owner's lists with every-owner retention, capping each owner apart", () => {
  const kept = createDeviceRecentList({
    storagePrefix: "omnigent:test-kept",
    field: "items",
    maxItems: 3,
    maxScopes: 1,
    item: z.string(),
    same: (left, right) => left === right,
    retention: "every-owner",
  });
  const stop = kept.subscribe(() => {});
  kept.record(["first"], ["a"], kept.owner());
  identityBecomes("user-2");
  kept.record(["first"], ["b"], kept.owner());
  kept.record(["second"], ["c"], kept.owner());
  expect(kept.read(["first"])).toEqual([]);
  expect(kept.read(["second"])).toEqual(["c"]);
  identityBecomes("user-1");
  expect(kept.read(["first"])).toEqual(["a"]);
  stop();
  kept.resetForTests();
});

it("tells subscribers about another window's change to this kind only", () => {
  const heard = vi.fn();
  const stop = list.subscribe(heard);
  window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:test-recent:branches:[]" }));
  expect(heard).not.toHaveBeenCalled();
  window.dispatchEvent(new StorageEvent("storage", { key: `${list.owner()}["x"]` }));
  window.dispatchEvent(new StorageEvent("storage", { key: null }));
  expect(heard).toHaveBeenCalledTimes(2);
  stop();
});

it("keeps each scope's items the same array while nothing changed, with two scopes shown", () => {
  list.record(["one"], ["a"], list.owner());
  list.record(["two"], ["b"], list.owner());
  const { result, rerender } = renderHook(() => [list.useItems(["one"]), list.useItems(["two"])]);
  const [one, two] = result.current;
  rerender();
  expect(result.current[0]).toBe(one);
  expect(result.current[1]).toBe(two);
});

it.each([
  ["server", () => (identity.server = "https://other.example.com")],
  ["API base path", () => (identity.base = "/other-api")],
])("keeps one %s's lists apart from another's, for the same user", (_case, move) => {
  list.record(SCOPE, ["a"], list.owner());
  const first = list.owner();
  move();
  expect(list.owner()).not.toBe(first);
  expect(list.read(SCOPE)).toEqual([]);
  list.record(SCOPE, ["b"], list.owner());
  expect(list.read(SCOPE)).toEqual(["b"]);
});

it("records nothing for an owner token taken before the server or user changed", () => {
  const before = list.owner();
  identity.server = "https://other.example.com";
  list.record(SCOPE, ["late"], before);
  expect(list.read(SCOPE)).toEqual([]);
  identity.server = "https://server.example.com";
  identity.user = "user-2";
  list.record(SCOPE, ["late"], before);
  expect(list.read(SCOPE)).toEqual([]);
  identity.user = "user-1";
  expect(list.read(SCOPE)).toEqual([]);
});
