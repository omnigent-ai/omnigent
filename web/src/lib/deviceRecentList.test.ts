// A device recent list puts what was just recorded first, the last of a batch first as the most
// recent, and moves an item recorded again to the front instead of keeping it twice.

import { afterEach, expect, it, vi } from "vitest";
import { z } from "zod";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

const identity = vi.hoisted(() => ({
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
  getOmnigentServerIdentity: () => "https://server.example.com",
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
