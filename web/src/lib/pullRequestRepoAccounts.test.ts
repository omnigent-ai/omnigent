// The repository account store: which login a repository's pull requests are read as, kept on
// this device per server and workspace user, holding nothing but the login.

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  userId: "user-1" as string | null,
  server: "server-1" as string | null,
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.userId,
  subscribeIdentity: () => () => {},
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => identity.server,
}));

import { repoReaderState } from "./pullRequestRepoReader";
import {
  MAX_REPO_ACCOUNTS,
  NO_REPO_READER,
  pullRequestRepository,
  repositoryKey,
  resetRepoAccountsForTests,
  savedRepoAccount,
  setRepoAccount,
  subscribeRepoAccounts,
} from "./pullRequestRepoAccounts";

const APP = { host: "github.com", owner: "acme", repo: "app" };
const LIB = { host: "github.com", owner: "acme", repo: "lib" };

beforeEach(() => {
  window.localStorage.clear();
  identity.userId = "user-1";
  identity.server = "server-1";
});

afterEach(() => {
  resetRepoAccountsForTests();
  window.localStorage.clear();
});

describe("pullRequestRepository", () => {
  it("names the repository of any page of a github.com pull request, the same for every spelling", () => {
    expect(pullRequestRepository("https://github.com/acme/app/pull/42")).toEqual(APP);
    expect(pullRequestRepository("https://GitHub.com/acme/app/pull/42/files?w=1#x")).toEqual(APP);
    expect(repositoryKey(pullRequestRepository("https://github.com/Acme/App/pull/7")!)).toBe(
      repositoryKey(APP),
    );
  });

  it("names nothing for other hosts and pages that aren't pull requests", () => {
    expect(pullRequestRepository("https://github.example.com/acme/app/pull/42")).toBeNull();
    expect(pullRequestRepository("https://github.com/acme/app/issues/42")).toBeNull();
    expect(pullRequestRepository(null)).toBeNull();
  });
});

describe("the saved choice", () => {
  it("is Automatic until a login is chosen, and Automatic again once it's cleared", () => {
    expect(savedRepoAccount(APP)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
    expect(setRepoAccount(APP, { mode: "account", login: "bob" })).toBe(true);
    expect(savedRepoAccount(APP)).toEqual({
      kind: "saved",
      intent: { mode: "account", login: "bob" },
    });
    // Another repository keeps its own choice.
    expect(savedRepoAccount(LIB)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
    expect(setRepoAccount(APP, { mode: "automatic" })).toBe(true);
    expect(savedRepoAccount(APP)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
  });

  it("keeps only the login, under a key that names the server and user, never a token", () => {
    setRepoAccount(APP, { mode: "account", login: "bob" });
    const entries = Object.entries(window.localStorage);
    expect(entries).toHaveLength(1);
    const [key, raw] = entries[0]!;
    expect(key).toContain("server-1");
    expect(key).toContain("user-1");
    const value = JSON.parse(raw) as Record<string, unknown>;
    expect(Object.keys(value).sort()).toEqual(["accounts", "usedAt"]);
    expect(value.accounts).toEqual([{ login: "bob" }]);
  });

  it("refuses a login gh couldn't have, and a damaged entry reads as Automatic", () => {
    expect(setRepoAccount(APP, { mode: "account", login: "-not a login" })).toBe(false);
    setRepoAccount(APP, { mode: "account", login: "bob" });
    const [key] = Object.keys(window.localStorage);
    window.localStorage.setItem(key!, '{"usedAt": 1, "accounts": [{"login": "-bad"}]}');
    expect(savedRepoAccount(APP)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
  });

  it("is each workspace user's own", () => {
    setRepoAccount(APP, { mode: "account", login: "bob" });
    identity.userId = "user-2";
    expect(savedRepoAccount(APP)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
  });

  it("stays each user's after another user chooses, and after their own repositories fill up", () => {
    setRepoAccount(APP, { mode: "account", login: "bob" });
    identity.userId = "user-2";
    setRepoAccount(APP, { mode: "account", login: "carol" });
    for (let index = 0; index < MAX_REPO_ACCOUNTS; index++) {
      setRepoAccount({ ...APP, repo: `repo-${index}` }, { mode: "account", login: "carol" });
    }
    identity.userId = "user-1";
    expect(savedRepoAccount(APP)).toEqual({
      kind: "saved",
      intent: { mode: "account", login: "bob" },
    });
  });

  it("is pending while the user isn't known and this device keeps choices, so nothing reads early", () => {
    identity.userId = null;
    // Nothing kept on this device: there's nothing to wait for.
    expect(savedRepoAccount(APP)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
    identity.userId = "user-1";
    setRepoAccount(APP, { mode: "account", login: "bob" });
    identity.userId = null;
    expect(savedRepoAccount(APP)).toEqual({ kind: "pending" });
    expect(setRepoAccount(APP, { mode: "automatic" })).toBe(false);
  });

  it("is Automatic, never pending, on a server with no stable identity, which keeps no choices", () => {
    setRepoAccount(APP, { mode: "account", login: "bob" });
    // An embedded server that names no identity, with a known user.
    identity.server = null;
    expect(savedRepoAccount(LIB)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
    expect(savedRepoAccount(APP)).toEqual({ kind: "saved", intent: { mode: "automatic" } });
    expect(repoReaderState("https://github.com/acme/lib/pull/1")).toEqual(NO_REPO_READER);
    // Automatic is already in effect; a login can't be kept here.
    expect(setRepoAccount(LIB, { mode: "automatic" })).toBe(true);
    expect(setRepoAccount(LIB, { mode: "account", login: "bob" })).toBe(false);
    // The choice made under the server that names itself is still there for it.
    identity.server = "server-1";
    expect(savedRepoAccount(APP)).toEqual({
      kind: "saved",
      intent: { mode: "account", login: "bob" },
    });
  });

  it(`keeps the ${MAX_REPO_ACCOUNTS} repositories used most recently`, () => {
    const now = vi.spyOn(Date, "now");
    for (let index = 0; index <= MAX_REPO_ACCOUNTS; index++) {
      now.mockReturnValue(1_000 + index);
      setRepoAccount({ ...APP, repo: `repo-${index}` }, { mode: "account", login: "bob" });
    }
    now.mockRestore();
    expect(savedRepoAccount({ ...APP, repo: "repo-0" })).toEqual({
      kind: "saved",
      intent: { mode: "automatic" },
    });
    expect(savedRepoAccount({ ...APP, repo: `repo-${MAX_REPO_ACCOUNTS}` })).toEqual({
      kind: "saved",
      intent: { mode: "account", login: "bob" },
    });
  });

  it("tells listeners about a choice made here and one made in another window", () => {
    const listener = vi.fn();
    const stop = subscribeRepoAccounts(listener);
    setRepoAccount(APP, { mode: "account", login: "bob" });
    expect(listener).toHaveBeenCalledTimes(1);
    window.dispatchEvent(
      new StorageEvent("storage", { key: "omnigent:pull-request-repo-account:other" }),
    );
    expect(listener).toHaveBeenCalledTimes(2);
    // Another kind of key isn't this store's.
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:something-else" }));
    expect(listener).toHaveBeenCalledTimes(2);
    stop();
  });
});
