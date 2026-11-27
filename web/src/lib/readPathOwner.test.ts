// The page's read-path owner is its first known user with the server identity and API base path;
// any later change of the three latches until a reload. A user not known yet is no owner.

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { afterEach, expect, it, vi } from "vitest";

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

import {
  onReadPathOwnerMoved,
  readPathOwnerMoved,
  resetReadPathOwnerForTests,
} from "./readPathOwner";

afterEach(() => {
  resetReadPathOwnerForTests();
  state.user = "user-1";
  state.server = "https://server-a.example.com";
  state.base = "/api";
});

const CHANGES: [string, string | null, () => void][] = [
  [
    "the server",
    "https://server-a.example.com",
    () => {
      state.server = "https://server-b.example.com";
    },
  ],
  [
    "the API base path",
    "https://server-a.example.com",
    () => {
      state.base = "/other/api";
    },
  ],
  [
    "the user",
    "https://server-a.example.com",
    () => {
      state.user = "user-2";
    },
  ],
  [
    "the base path with no server identity",
    null,
    () => {
      state.base = "/two";
    },
  ],
];

it.each(CHANGES)(
  "moves when %s changes, and stays moved back on the first owner",
  (_case, server, change) => {
    state.server = server;
    expect(readPathOwnerMoved()).toBe(false);
    change();
    expect(readPathOwnerMoved()).toBe(true);
    state.user = "user-1";
    state.server = server;
    state.base = "/api";
    expect(readPathOwnerMoved()).toBe(true);
  },
);

it("takes the first known user as the owner, and never moves on a user not known yet", () => {
  state.user = null;
  expect(readPathOwnerMoved()).toBe(false);
  state.user = "user-1";
  expect(readPathOwnerMoved()).toBe(false);
  state.user = null;
  expect(readPathOwnerMoved()).toBe(false);
  state.user = "user-1";
  expect(readPathOwnerMoved()).toBe(false);
});

it("tells listeners once, after the read that found the move", async () => {
  const moved = vi.fn();
  const stop = onReadPathOwnerMoved(moved);
  readPathOwnerMoved();
  state.server = "https://server-b.example.com";
  readPathOwnerMoved();
  readPathOwnerMoved();
  expect(moved).not.toHaveBeenCalled();
  await Promise.resolve();
  expect(moved).toHaveBeenCalledTimes(1);
  stop();
});
