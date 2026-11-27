// The read path serves only the owner its page started with: server identity, API base path and
// workspace user. After any change of owner, every PR reads through the session's host until a
// reload, with no Mac read kept and no per-owner choice applied, even back on the first owner.

import type * as BasePathModule from "@/lib/basePath";
import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { afterEach, expect, it, vi } from "vitest";

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
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macOffers,
  macReadTarget,
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
  acceptMacRead,
  beginMacRead,
  macReadCurrent,
  macReadScopeKey,
  resetMacReadScopesForTests,
} from "./macReadScope";
import { resetRepoAccountsForTests, setRepoAccount } from "./pullRequestRepoAccounts";
import { repoReaderState } from "./pullRequestRepoReader";

const PR = "https://github.com/example-org/example-repo/pull/7";
const OTHER_PR = "https://github.com/example-org/other-repo/pull/8";
const REPOSITORY = { host: "github.com", owner: "example-org", repo: "example-repo" };
const READER = { host: "github.com", login: "bob" };

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

it("keeps no named-read proof or Mac read after A, then B read only for a route, then A again", async () => {
  await namedReadsOn();
  expect(acceptNamedAnswer(namedReadTicket(READER), namedAnswer("bob"))).toBe("accepted");
  const proof = namedRestoreProof(READER, "bob")!;
  expect(namedRestoreProofHolds(READER, proof)).toBe(true);
  const key = macReadScopeKey("github", "github.com");
  const pending = beginMacRead(key);

  // On B, only a route is asked.
  state.server = SERVER_B;
  readRouteFor(PR);

  state.server = SERVER_A;
  expect(namedRestoreProofHolds(READER, proof)).toBe(false);
  expect(namedRestoreProof(READER, "bob")).toBeNull();
  expect(namedWriterIdentity(READER)).toBe("checking");
  expect(macReadCurrent(pending)).toBe(false);
  expect(acceptMacRead(pending, "alice")).toBe("rejected");
});

it("applies no PR read choice after the API base path changes on a server with no identity", async () => {
  state.server = null;
  state.base = "/one";
  await namedReadsOn();
  chooseReadSource(PR, "session");
  expect(readChoiceFor(PR)).toBe("session");

  state.base = "/two";
  expect(readChoiceFor(PR)).toBe("automatic");
  // Choosing again applies nothing either, until a reload.
  chooseReadSource(PR, "mac");
  expect(readChoiceFor(PR)).toBe("automatic");
  state.base = "/one";
  expect(readChoiceFor(PR)).toBe("automatic");
});

it("reads every PR through the session's host after a switch, and keeps doing so back on the first server", async () => {
  await namedReadsOn();
  githubMacReadsPreference.write("on");
  expect(setRepoAccount(REPOSITORY, { mode: "account", login: "bob" })).toBe(true);
  chooseReadSource(OTHER_PR, "mac");
  const AUTOMATIC_PR = "https://github.com/example-org/third-repo/pull/9";
  // On A: the saved login, the explicit choice and Automatic each read on this Mac.
  await vi.waitFor(() => expect(readRouteFor(PR)).toMatchObject({ kind: "mac" }));
  expect(readRouteFor(OTHER_PR)).toMatchObject({ kind: "mac" });
  expect(readRouteFor(AUTOMATIC_PR)).toMatchObject({ kind: "mac" });

  for (const server of [SERVER_B, SERVER_A]) {
    state.server = server;
    const urls = [PR, OTHER_PR, AUTOMATIC_PR];
    for (const url of urls) {
      expect(readRouteFor(url), `${server} ${url}`).toEqual({ kind: "host" });
      expect(macReadTarget(url)).toBeNull();
      expect(macReadTargetAs(url, READER)).toBeNull();
    }
    // oxlint-disable-next-line no-await-in-loop -- each server's reads are checked in turn
    expect(await Promise.all(urls.map((url) => desktopReadTarget(url)))).toEqual([
      null,
      null,
      null,
    ]);
    expect(repoReaderState(PR)).toEqual({ kind: "none" });
    expect(macOffers("github", "pull_request_inbox")).toBe(false);
    expect(namedReadsAvailable()).toBe(false);
  }
});
