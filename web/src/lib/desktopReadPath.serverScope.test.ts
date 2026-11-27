// What the read path keeps for a workspace user belongs to that user on one server and API base
// path: a user id names someone only on its own server. Each state switches A → B → A here, with
// the same user id, since no event says the server changed.

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { afterEach, describe, expect, it, vi } from "vitest";

const SERVER_A = "https://server-a.example.com";
const SERVER_B = "https://server-b.example.com";
const state = vi.hoisted(() => ({
  server: "https://server-a.example.com" as string | null,
  base: "/api",
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
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
  chooseReadSource,
  discoverMacIdentity,
  findBranchOnSessionMachine,
  findsBranchOnSessionMachine,
  GITHUB_MAC_READ_OPERATIONS,
  macIdentity,
  macReadTargetAs,
  readChoiceFor,
  readRouteFor,
  resetDesktopReadPathForTests,
} from "./desktopReadPath";
import {
  acceptNamedAnswer,
  namedReadsAvailable,
  namedReadTicket,
  namedRestoreProof,
  namedRestoreProofHolds,
  namedWriterIdentity,
  PULL_REQUESTS_PAGE_ACCOUNTS_FLAG,
  resetMacNamedReadsForTests,
} from "./macNamedReads";
import {
  acceptedMacReader,
  acceptMacRead,
  beginMacRead,
  macNamedReadScopeKey,
  macReadScopeKey,
  onMacReadScopeAdvanced,
  resetMacReadScopesForTests,
} from "./macReadScope";
import { resetRepoAccountsForTests, setRepoAccount } from "./pullRequestRepoAccounts";

const PR = "https://github.com/example-org/example-repo/pull/7";
const REPOSITORY = { host: "github.com", owner: "example-org", repo: "example-repo" };
const READER = { host: "github.com", login: "bob" };

/** A shell that reads GitHub on this Mac and by name, with the page's accounts flag on. */
async function namedReadsOn() {
  let setFlag: ((value: unknown) => void) | null = null;
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: async () => ({ ok: false }),
    codeHostAccounts: {
      // This Mac reads GitHub as `alice` when no login is named.
      get: async () => ({
        ok: true,
        data: {
          intent: { mode: "automatic" },
          effective: { login: "alice", generation: 1 },
          accounts: [{ login: "alice", active: true, state: "success" }],
        },
      }),
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

const namedAnswer = (login: string) => ({
  identity: { host: "github.com", login, generation: 0, named: true },
  provenance: { version: 1, provider: "github", host: "github.com", viewer: login },
});

afterEach(() => {
  resetRepoAccountsForTests();
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  window.localStorage.clear();
  state.server = SERVER_A;
  state.base = "/api";
});

describe("routes and read choices", () => {
  it("routes a repository's chosen login only on its own server, not after a switch to one with no identity", async () => {
    await namedReadsOn();
    expect(setRepoAccount(REPOSITORY, { mode: "account", login: "bob" })).toBe(true);
    await vi.waitFor(() => expect(readRouteFor(PR)).toMatchObject({ kind: "mac" }));
    expect(macReadTargetAs(PR, null)).toMatchObject({ readAs: "bob" });

    // An embedded backend with no stable identity, for the same user: no choice applies there.
    state.server = null;
    expect(readRouteFor(PR)).toEqual({ kind: "host" });
    expect(macReadTargetAs(PR, null)?.readAs).toBeUndefined();

    state.server = SERVER_A;
    expect(readRouteFor(PR)).toMatchObject({ kind: "mac" });
  });

  it("keeps a PR's read choice on the server it was made on, so another's saved login applies there", async () => {
    await namedReadsOn();
    state.server = SERVER_B;
    expect(setRepoAccount(REPOSITORY, { mode: "account", login: "carol" })).toBe(true);
    state.server = SERVER_A;
    chooseReadSource(PR, "session");
    expect(readRouteFor(PR)).toEqual({ kind: "host" });

    // Server B's own saved login reads the PR, not server A's choice of the session's machine.
    state.server = SERVER_B;
    expect(readChoiceFor(PR)).toBe("automatic");
    await vi.waitFor(() => expect(readRouteFor(PR)).toMatchObject({ kind: "mac" }));
    expect(macReadTargetAs(PR, null)).toMatchObject({ readAs: "carol" });

    // Back on A, its choice was for that visit: the PR is on Automatic again.
    state.server = SERVER_A;
    expect(readChoiceFor(PR)).toBe("automatic");
  });

  it("drops a read choice when only the API base path changes", async () => {
    await namedReadsOn();
    chooseReadSource(PR, "session");
    state.base = "/other/api";
    expect(readChoiceFor(PR)).toBe("automatic");
    state.base = "/api";
    expect(readChoiceFor(PR)).toBe("automatic");
  });
});

it("forgets which sessions find their branch on the session's machine at a server switch", async () => {
  await namedReadsOn();
  findBranchOnSessionMachine("conv_1");
  expect(findsBranchOnSessionMachine("conv_1")).toBe(true);
  state.server = SERVER_B;
  expect(findsBranchOnSessionMachine("conv_1")).toBe(false);
  state.server = SERVER_A;
  expect(findsBranchOnSessionMachine("conv_1")).toBe(false);
});

it("forgets the account this Mac reads a host as at a server switch, until asked again", async () => {
  await namedReadsOn();
  expect(await discoverMacIdentity("github.com")).toBe("alice");
  state.server = SERVER_B;
  expect(macIdentity("github.com")).toBeNull();
  state.server = SERVER_A;
  expect(macIdentity("github.com")).toBeNull();
  expect(await discoverMacIdentity("github.com")).toBe("alice");
});

it("refuses a Mac read sent on another server, and drops what each scope showed", async () => {
  await namedReadsOn();
  const key = macReadScopeKey("github", "github.com");
  const advanced: unknown[] = [];
  const stop = onMacReadScopeAdvanced((moved) => advanced.push(moved));
  expect(acceptMacRead(beginMacRead(key), "alice")).toBe("accepted");
  const sentOnA = beginMacRead(key);

  state.server = SERVER_B;
  expect(acceptMacRead(sentOnA, "alice")).toBe("rejected");
  expect(acceptedMacReader(key)).toBeNull();
  // Queries cached under the scope are dropped once the read that found the switch is done.
  expect(advanced).toEqual([]);
  await Promise.resolve();
  expect(advanced).toEqual([key]);

  state.server = SERVER_A;
  expect(acceptMacRead(sentOnA, "alice")).toBe("rejected");
  expect(acceptMacRead(beginMacRead(key), "alice")).toBe("accepted");
  stop();
});

it("proves no named writer or restore after a server switch, even back on the first", async () => {
  await namedReadsOn();
  expect(acceptNamedAnswer(namedReadTicket(READER), namedAnswer("bob"))).toBe("accepted");
  const proof = namedRestoreProof(READER, "bob")!;
  expect(namedWriterIdentity(READER)).toEqual({ login: "bob", generation: 0 });

  state.server = SERVER_B;
  expect(namedWriterIdentity(READER)).toBe("checking");
  expect(namedRestoreProofHolds(READER, proof)).toBe(false);

  state.server = SERVER_A;
  expect(namedWriterIdentity(READER)).toBe("checking");
  expect(namedRestoreProofHolds(READER, proof)).toBe(false);
  expect(namedRestoreProof(READER, "bob")).toBeNull();
});

it("never retires a login a PR read as on another server, when a choice changes on this one", async () => {
  await namedReadsOn();
  expect(setRepoAccount(REPOSITORY, { mode: "account", login: "bob" })).toBe(true);
  await vi.waitFor(() => expect(readRouteFor(PR)).toMatchObject({ kind: "mac" }));
  const moves: string[] = [];
  const bob = JSON.stringify(macNamedReadScopeKey("github.com", "bob"));
  const stop = onMacReadScopeAdvanced((key, move) => {
    if (JSON.stringify(key) === bob) moves.push(move);
  });

  // On B, nothing reads the PR before another repository's choice changes.
  state.server = SERVER_B;
  setRepoAccount({ ...REPOSITORY, repo: "other-repo" }, { mode: "account", login: "carol" });
  await Promise.resolve();
  expect(moves).not.toContain("left");

  state.server = SERVER_A;
  setRepoAccount({ ...REPOSITORY, repo: "other-repo" }, { mode: "automatic" });
  await Promise.resolve();
  expect(moves).not.toContain("left");
  stop();
});
