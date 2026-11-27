// The page's read-path server is its server identity and API base path once a user is known; a
// later change of either latches until a reload. A user switch on the same server never does.

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
  onReadPathServerMoved,
  readPathServerMoved,
  resetReadPathServerForTests,
} from "./readPathServer";

afterEach(() => {
  resetReadPathServerForTests();
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
    "the base path with no server identity",
    null,
    () => {
      state.base = "/two";
    },
  ],
];

it.each(CHANGES)(
  "moves when %s changes, and stays moved back on the first server",
  (_case, server, change) => {
    state.server = server;
    expect(readPathServerMoved()).toBe(false);
    change();
    expect(readPathServerMoved()).toBe(true);
    state.server = server;
    state.base = "/api";
    expect(readPathServerMoved()).toBe(true);
  },
);

it("takes the first server once a user is known, and never moves on a user not known yet", () => {
  state.user = null;
  state.server = "https://server-b.example.com";
  expect(readPathServerMoved()).toBe(false);
  state.user = "user-1";
  state.server = "https://server-a.example.com";
  expect(readPathServerMoved()).toBe(false);
  state.user = null;
  expect(readPathServerMoved()).toBe(false);
  state.user = "user-1";
  expect(readPathServerMoved()).toBe(false);
});

it("stays open when the user switches on the same server", () => {
  const moved = vi.fn();
  const stop = onReadPathServerMoved(moved);
  expect(readPathServerMoved()).toBe(false);
  state.user = "user-2";
  expect(readPathServerMoved()).toBe(false);
  state.user = "user-1";
  expect(readPathServerMoved()).toBe(false);
  expect(moved).not.toHaveBeenCalled();
  stop();
});

it("tells listeners once, after the read that found the move", async () => {
  const moved = vi.fn();
  const stop = onReadPathServerMoved(moved);
  readPathServerMoved();
  state.server = "https://server-b.example.com";
  readPathServerMoved();
  readPathServerMoved();
  expect(moved).not.toHaveBeenCalled();
  await Promise.resolve();
  expect(moved).toHaveBeenCalledTimes(1);
  stop();
});
