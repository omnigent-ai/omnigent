// A PR's route follows the server it's read through: a repository's login chosen on one server
// never routes a read after a switch to a backend with no stable identity, even for the same user.

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { afterEach, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({ server: "https://server-a.example.com" as string | null }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => state.server,
}));

import {
  GITHUB_MAC_READ_OPERATIONS,
  macReadTargetAs,
  readRouteFor,
  resetDesktopReadPathForTests,
} from "./desktopReadPath";
import {
  namedReadsAvailable,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "./macNamedReads";
import { resetMacReadScopesForTests } from "./macReadScope";
import { resetRepoAccountsForTests, setRepoAccount } from "./pullRequestRepoAccounts";

const PR = "https://github.com/example-org/example-repo/pull/7";
const REPOSITORY = { host: "github.com", owner: "example-org", repo: "example-repo" };

/** A shell that reads GitHub on this Mac and by name, with the page's accounts flag on. */
async function namedReadsOn() {
  let setFlag: ((value: unknown) => void) | null = null;
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: async () => ({ ok: false }),
    codeHostAccounts: {
      get: async () => ({ ok: false }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_inbox"],
          accounts: true,
          answerProvenance: 1,
          readAs: 1,
        },
      ],
      featureFlags: { names: [PULL_REQUESTS_PAGE_ACCOUNTS_FLAG] },
    }),
    featureFlags: {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === PULL_REQUESTS_PAGE_ACCOUNTS_FLAG) setFlag = callback;
        return () => {};
      },
    },
  };
  namedReadsAvailable();
  await vi.waitFor(() => expect(setFlag).not.toBeNull());
  setFlag!(true);
  await vi.waitFor(() => expect(namedReadsAvailable()).toBe(true));
}

afterEach(() => {
  resetRepoAccountsForTests();
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
  state.server = "https://server-a.example.com";
});

it("routes a repository's chosen login only on its own server, not after a switch to one with no identity", async () => {
  await namedReadsOn();
  expect(setRepoAccount(REPOSITORY, { mode: "account", login: "bob" })).toBe(true);
  await vi.waitFor(() => expect(readRouteFor(PR)).toMatchObject({ kind: "mac" }));
  expect(macReadTargetAs(PR, null)).toMatchObject({ readAs: "bob" });

  // An embedded backend with no stable identity, for the same user: no choice applies there.
  state.server = null;
  expect(readRouteFor(PR)).toEqual({ kind: "host" });
  expect(macReadTargetAs(PR, null)?.readAs).toBeUndefined();

  state.server = "https://server-a.example.com";
  expect(readRouteFor(PR)).toMatchObject({ kind: "mac" });
});
