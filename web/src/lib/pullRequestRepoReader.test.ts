// A repository's chosen login on this Mac: every pull request of it is read and changed as that
// login, or by nobody while it can't be; never as the host's account or the session's machine.
// Covers precedence, the shell's capability, and readers changing while reads are in flight.

import type * as HostModule from "@/lib/host";
import type * as IdentityModule from "@/lib/identity";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));
vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof HostModule>()),
  getOmnigentServerIdentity: () => "https://omnigent.example.com",
}));

import {
  beginMacWrite,
  macWriteDispatch,
  resetDesktopPullRequestWritesForTests,
  sendMacWrite,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { readOnThisMac } from "@/lib/desktopPullRequests";
import {
  assertHostReadAllowed,
  chooseReadSource,
  GITHUB_MAC_READ_OPERATIONS,
  macReadTargetAs,
  readRouteFor,
  repoReaderFor,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  NAMED_READ_OPERATIONS,
  NAMED_WRITE_READS,
  resetMacNamedReadsForTests,
} from "@/lib/macNamedReads";
import { macNamedReadScopeKey, macReadEpoch, resetMacReadScopesForTests } from "@/lib/macReadScope";
import {
  resetAcceptedWorkspaceDesignForTests,
  setAcceptedWorkspaceDesign,
} from "@/lib/workspaceSurfaces";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { resetRepoAccountsForTests, setRepoAccount } from "./pullRequestRepoAccounts";
import { resetRepoReaderForTests } from "./pullRequestRepoReader";

const PR = "https://github.com/acme/app/pull/42";
const SAME_REPO_PR = "https://github.com/acme/app/pull/7";
const OTHER_REPO_PR = "https://github.com/acme/lib/pull/3";
const APP = { host: "github.com", owner: "acme", repo: "app" };
const LIB = { host: "github.com", owner: "acme", repo: "lib" };
const PARAMS = { repository: "acme/app", number: 42 };
const CAROL = { host: "github.com", login: "carol" };
const COMMENT = {
  ok: true,
  data: { comment: { author: "bob", body: "hi", created_at: null, url: `${PR}#c1` } },
  actor: { login: "bob" },
};
// A read main runs as the host's account only, never by name.
const HOST_ONLY_READ = GITHUB_MAC_READ_OPERATIONS.find(
  (operation) => !NAMED_READ_OPERATIONS.has(operation) && !NAMED_WRITE_READS.has(operation),
)!;

type Shell = ReturnType<typeof installNamedWriteShell>;

/** Who each of the shell's reads of `operation` went out as: a login by name, or the host's. */
function sentAs(shell: Shell, operation = "pull_request_overview"): string[] {
  return shell.codeHostQuery.mock.calls
    .map(([request]) => request)
    .filter((request) => request.operation === operation)
    .map((request) => (typeof request.macLogin === "string" ? request.macLogin : "host"));
}

const bobEpoch = () => macReadEpoch(macNamedReadScopeKey("github.com", "bob", "user-1"));

/** The workspace user becomes `id`, as identity tells its subscribers. */
function signInAs(id: string | null) {
  currentUser.id = id;
  for (const listener of [...identityListeners]) listener();
}

const readOverview = (url: string) =>
  readOnThisMac(macReadTargetAs(url, null)!, "pull_request_overview", PARAMS);

/** A shell reading as `alice` (gh's active account) and by name, with acme/app chosen as `bob`. */
async function bobForApp(options: Parameters<typeof installNamedWriteShell>[1] = {}) {
  const shell = installNamedWriteShell(() => COMMENT, options);
  await shell.ready(PR, { asBob: false });
  expect(setRepoAccount(APP, { mode: "account", login: "bob" })).toBe(true);
  shell.codeHostQuery.mockClear();
  return shell;
}

beforeEach(() => {
  window.localStorage.clear();
  currentUser.id = "user-1";
});

afterEach(() => {
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetRepoReaderForTests();
  resetRepoAccountsForTests();
  resetAcceptedWorkspaceDesignForTests();
  removeNamedWriteShell();
  window.localStorage.clear();
  identityListeners.clear();
});

describe("a repository with a chosen login", () => {
  it("reads each of its pull requests as that login, while another repository reads as the host's", async () => {
    const shell = await bobForApp();
    expect(repoReaderFor(PR)).toEqual({ kind: "named", reader: BOB });
    await readOverview(PR);
    await readOverview(SAME_REPO_PR);
    await readOverview(OTHER_REPO_PR);
    expect(sentAs(shell)).toEqual(["bob", "bob", "host"]);
    // Its answers are kept in the login's own scope, apart from the host's account's.
    expect(macReadTargetAs(PR, null)?.readAs).toBe("bob");
    expect(macReadTargetAs(OTHER_REPO_PR, null)?.readAs).toBeUndefined();
  });

  it("never sends a read main runs only as the host's account", async () => {
    const shell = await bobForApp();
    await expect(readOnThisMac(macReadTargetAs(PR, null)!, HOST_ONLY_READ, PARAMS)).rejects.toThrow(
      "can’t read it as @bob",
    );
    expect(sentAs(shell, HOST_ONLY_READ)).toEqual([]);
  });

  it("makes its changes as that login through codeHostWriteAs", async () => {
    const shell = await bobForApp();
    await readOverview(PR);
    const dispatch = macWriteDispatch(PR, null);
    expect(dispatch.kind).toBe("mac");
    if (dispatch.kind !== "mac") return;
    expect(dispatch.route.actor.login).toBe("bob");
    await sendMacWrite(
      beginMacWrite(dispatch.route),
      "pull_request_comment",
      { repository: "acme/app", number: 42, body: "hi" },
      (data) => data,
    );
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({ macLogin: "bob" });
  });
});

describe("precedence", () => {
  it("a view's own login wins over the repository's", async () => {
    const shell = await bobForApp();
    await readOnThisMac(macReadTargetAs(PR, CAROL)!, "pull_request_overview", PARAMS);
    expect(sentAs(shell)).toEqual(["carol"]);
  });

  it("Session's machine, chosen for a PR, reads it there without the repository's login", async () => {
    const shell = await bobForApp();
    chooseReadSource(PR, "session");
    expect(readRouteFor(PR)).toEqual({ kind: "host" });
    expect(repoReaderFor(PR)).toEqual({ kind: "none" });
    expect(() => assertHostReadAllowed(PR)).not.toThrow();
    // Another PR of the repository still reads as its login.
    await readOverview(SAME_REPO_PR);
    expect(sentAs(shell)).toEqual(["bob"]);
  });
});

describe("while the chosen login can't be used, nothing reads or changes the repository", () => {
  function expectNothingSent(shell: Shell) {
    expect(readRouteFor(PR)).toEqual({ kind: "blocked", reason: "repo_account" });
    expect(macReadTargetAs(PR, null)).toBeNull();
    // Nor through the session's machine.
    expect(() => assertHostReadAllowed(PR)).toThrow();
    expect(sentAs(shell)).toEqual([]);
  }

  it("on a shell that can't read as a named login", async () => {
    const shell = await bobForApp();
    await shell.loseNamedSupport();
    expect(repoReaderFor(PR)).toMatchObject({ kind: "blocked", reason: "update_needed" });
    expectNothingSent(shell);
    expect(macWriteDispatch(PR, null)).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "named_writes_need_update", reader: BOB },
    });
  });

  it("when the release flag turns off while the PR is open", async () => {
    const shell = await bobForApp();
    await readOverview(PR);
    shell.codeHostQuery.mockClear();
    shell.flag.set!(false);
    await vi.waitFor(() =>
      expect(repoReaderFor(PR)).toMatchObject({ kind: "blocked", reason: "turned_off" }),
    );
    expectNothingSent(shell);
    expect(macWriteDispatch(PR, null)).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "named_reads_off", reader: BOB },
    });
  });

  it("before the shell has said what it supports", async () => {
    const shell = installNamedWriteShell(() => COMMENT);
    setRepoAccount(APP, { mode: "account", login: "bob" });
    expect(repoReaderFor(PR)).toMatchObject({ kind: "blocked", reader: BOB });
    expectNothingSent(shell);
    expect(macWriteDispatch(PR, null)).toMatchObject({ kind: "blocked" });
  });

  it("before the workspace user is known, where this device keeps choices", async () => {
    const shell = await bobForApp();
    currentUser.id = null;
    for (const listener of identityListeners) listener();
    expect(repoReaderFor(PR)).toEqual({ kind: "blocked", reason: "pending", reader: null });
    // Every github.com PR waits, since whose choice applies isn't known.
    expect(readRouteFor(OTHER_REPO_PR)).toEqual({ kind: "blocked", reason: "repo_account" });
    expectNothingSent(shell);
    expect(macWriteDispatch(PR, null)).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "account_checking", reader: null },
    });
  });

  it("on a partial shell, reads still go as the login and changes are blocked", async () => {
    const shell = await bobForApp({ writeAs: false });
    await readOverview(PR);
    expect(sentAs(shell)).toEqual(["bob"]);
    expect(macWriteDispatch(PR, null)).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "named_writes_need_update", reader: BOB },
    });
  });

  it("after the login signs out, with no read retried as another account", async () => {
    const shell = await bobForApp();
    await readOverview(PR);
    shell.state.bobSignedIn = false;
    await expect(readOverview(PR)).rejects.toThrow();
    expect(macWriteDispatch(PR, null)).toMatchObject({
      kind: "blocked",
      unavailable: { reason: "no_account", reader: BOB },
    });
    expect(sentAs(shell)).toEqual(["bob", "bob"]);
  });
});

describe("changing the chosen login", () => {
  it("A to B and back to A refuses a read sent as A before the switch, and reads again", async () => {
    const shell = await bobForApp();
    await readOverview(PR);
    const before = macReadEpoch(macNamedReadScopeKey("github.com", "bob", "user-1"));
    shell.codeHostQuery.mockClear();
    const held = shell.pauseNextRead("pull_request_overview");
    const inFlight = readOverview(PR);
    await held.started;
    setRepoAccount(APP, { mode: "account", login: "carol" });
    setRepoAccount(APP, { mode: "account", login: "bob" });
    held.release();
    await inFlight;
    // The held answer was refused, so the same login read again: a fresh answer shows.
    expect(sentAs(shell)).toEqual(["bob", "bob"]);
    // What was kept as bob before the switch belongs to an older record.
    expect(macReadEpoch(macNamedReadScopeKey("github.com", "bob", "user-1"))).toBeGreaterThan(
      before,
    );
  });

  it("another repository still on the left login reads again once, as it", async () => {
    const shell = await bobForApp();
    setRepoAccount(LIB, { mode: "account", login: "bob" });
    // Both repositories read as bob.
    await readOverview(PR);
    await readOverview(OTHER_REPO_PR);
    shell.codeHostQuery.mockClear();
    const held = shell.pauseNextRead("pull_request_overview");
    const inFlight = readOverview(OTHER_REPO_PR);
    await held.started;
    // acme/app leaves bob; acme/lib keeps him, but his reads in flight were sent before.
    setRepoAccount(APP, { mode: "automatic" });
    held.release();
    await inFlight;
    expect(sentAs(shell)).toEqual(["bob", "bob"]);
  });

  it("follows a choice made in another window", async () => {
    const shell = await bobForApp();
    const [key, raw] = Object.entries(window.localStorage).find(([name]) =>
      name.startsWith("omnigent:pull-request-repo-account:"),
    )!;
    // The other window cleared the choice; this one hears it through storage.
    window.localStorage.removeItem(key);
    window.dispatchEvent(new StorageEvent("storage", { key, oldValue: raw, newValue: null }));
    expect(repoReaderFor(PR)).toEqual({ kind: "none" });
    await readOverview(PR);
    expect(sentAs(shell)).toEqual(["host"]);
  });

  it("is each workspace user's own: another user reads the repository as the host's account", async () => {
    await bobForApp();
    currentUser.id = "user-2";
    for (const listener of identityListeners) listener();
    expect(repoReaderFor(PR)).toEqual({ kind: "none" });
    expect(macReadTargetAs(PR, null)?.readAs).toBeUndefined();
  });
});

describe("the workspace user changing keeps each user's choice", () => {
  it("unknown, then the same user again: waits meanwhile, then reads as the login again", async () => {
    const shell = await bobForApp();
    await readOverview(PR);
    shell.codeHostQuery.mockClear();
    signInAs(null);
    expect(repoReaderFor(PR)).toEqual({ kind: "blocked", reason: "pending", reader: null });
    expect(() => assertHostReadAllowed(PR)).toThrow();
    signInAs("user-1");
    expect(repoReaderFor(PR)).toEqual({ kind: "named", reader: BOB });
    await readOverview(PR);
    expect(sentAs(shell)).toEqual(["bob"]);
  });

  it("user A, user B, then A again: B has no choice of their own, and A reads as theirs", async () => {
    const shell = await bobForApp();
    signInAs("user-2");
    expect(repoReaderFor(PR)).toEqual({ kind: "none" });
    signInAs("user-1");
    expect(repoReaderFor(PR)).toEqual({ kind: "named", reader: BOB });
    await readOverview(PR);
    expect(sentAs(shell)).toEqual(["bob"]);
  });
});

describe("Classic, which reads a This Mac choice through the session's machine", () => {
  it.each([
    ["This Mac", "before", "mac"],
    ["This Mac", "after", "mac"],
    ["Automatic", "before", "automatic"],
    ["Automatic", "after", "automatic"],
  ] as const)(
    "still reads a repository's login with %s chosen for the PR, design changed %s the login was picked",
    async (_name, order, choice) => {
      const shell = installNamedWriteShell(() => COMMENT);
      await shell.ready(PR, { asBob: false });
      setAcceptedWorkspaceDesign("tabs");
      chooseReadSource(PR, choice);
      if (order === "before") setAcceptedWorkspaceDesign("classic");
      expect(setRepoAccount(APP, { mode: "account", login: "bob" })).toBe(true);
      if (order === "after") setAcceptedWorkspaceDesign("classic");
      shell.codeHostQuery.mockClear();
      expect(readRouteFor(PR)).toMatchObject({ kind: "mac" });
      await readOverview(PR);
      expect(sentAs(shell)).toEqual(["bob"]);
    },
  );
});

describe("changing where a PR is read retires its login's answers", () => {
  it("This Mac, Session's machine, then This Mac again refuses a read sent before, and reads again", async () => {
    const shell = await bobForApp();
    await readOverview(PR);
    const before = bobEpoch();
    shell.codeHostQuery.mockClear();
    const held = shell.pauseNextRead("pull_request_overview");
    const inFlight = readOverview(PR);
    await held.started;
    chooseReadSource(PR, "session");
    chooseReadSource(PR, "mac");
    held.release();
    await inFlight;
    expect(sentAs(shell)).toEqual(["bob", "bob"]);
    // What was kept as bob before belongs to an older record.
    expect(bobEpoch()).toBeGreaterThan(before);
  });

  it("switching designs keeps the login, so a read in flight still counts and nothing goes elsewhere", async () => {
    const shell = await bobForApp();
    setAcceptedWorkspaceDesign("tabs");
    chooseReadSource(PR, "mac");
    await readOverview(PR);
    shell.codeHostQuery.mockClear();
    const held = shell.pauseNextRead("pull_request_overview");
    const inFlight = readOverview(PR);
    await held.started;
    setAcceptedWorkspaceDesign("classic");
    expect(readRouteFor(PR)).toMatchObject({ kind: "mac" });
    setAcceptedWorkspaceDesign("tabs");
    held.release();
    await inFlight;
    expect(sentAs(shell)).toEqual(["bob"]);
  });
});
