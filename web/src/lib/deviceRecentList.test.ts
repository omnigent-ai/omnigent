// A device recent list puts what was just recorded first, the last of a batch first as the most
// recent, and moves an item recorded again to the front instead of keeping it twice.

import { afterEach, expect, it, vi } from "vitest";
import { z } from "zod";

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
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

afterEach(() => window.localStorage.clear());

it("puts a batch first, its last item first, and keeps the list to its limit", () => {
  list.record(SCOPE, ["a"], list.owner());
  list.record(SCOPE, ["b", "c"], list.owner());
  expect(list.read(SCOPE)).toEqual(["c", "b", "a"]);
  list.record(SCOPE, ["a", "d"], list.owner());
  expect(list.read(SCOPE)).toEqual(["d", "a", "c"]);
});
