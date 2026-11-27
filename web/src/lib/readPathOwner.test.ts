// The read path's owner is the workspace user on one server and API base path; a watch runs its
// store's switch once per change of any of the three, and never on the first look.

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  user: "user-1" as string | null,
  server: "https://server-a.example.com" as string | null,
  base: "/api",
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => state.user,
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));
vi.mock("@/lib/basePath", async (importOriginal) => ({
  ...(await importOriginal<typeof BasePathModule>()),
  getApiBasePath: () => state.base,
}));

import { createReadPathOwnerWatch, readPathOwner } from "./readPathOwner";

it("tells one user id on two servers or base paths apart", () => {
  const onA = readPathOwner();
  state.server = "https://server-b.example.com";
  expect(readPathOwner()).not.toBe(onA);
  state.server = "https://server-a.example.com";
  state.base = "/other/api";
  expect(readPathOwner()).not.toBe(onA);
  state.base = "/api";
  expect(readPathOwner()).toBe(onA);
});

it("runs a store's switch once per change, as it was found, and not on the first look", () => {
  const onSwitch = vi.fn();
  const watch = createReadPathOwnerWatch(onSwitch);
  expect(watch()).toBe(false);
  expect(watch()).toBe(false);
  state.server = "https://server-b.example.com";
  expect(watch()).toBe(true);
  expect(watch()).toBe(false);
  state.user = "user-2";
  expect(watch("event")).toBe(true);
  expect(onSwitch.mock.calls).toEqual([["lazy"], ["event"]]);
  // A reset starts over: the next look sees no change.
  watch.forget();
  state.server = "https://server-a.example.com";
  expect(watch()).toBe(false);
  expect(onSwitch).toHaveBeenCalledTimes(2);
});
