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

/** The workspace's identity check settles as `id`, as `/v1/me` would tell every listener. */
function signInAs(id: string | null) {
  currentUser.id = id;
  for (const listener of [...identityListeners]) listener();
}

import {
  beginMacWrite,
  cachedUnderTicket,
  GITHUB_WRITE_SET_V1,
  macWriteDoneMessage,
  macWriteMayPatch,
  macWriteRoute,
  macCouldWriteOnceReadHere,
  macWriteUnavailable,
  macWritesEnabled,
  recheckMacWrites,
  MacWriteError,
  PAGE_WRITE_OPERATIONS,
  pageWritesComplete,
  parseCommentAnswer,
  parseReplyAnswer,
  parseThreadAnswer,
  readMacWritesIntent,
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
  setPageWriteOperationsForTests,
  shellWritesComplete,
  writeMacWritesIntent,
  currentWorkspaceAuthGeneration,
  MAC_WRITE_SUPPRESSED,
  macWriteTicketCurrent,
  sendMacWrite,
  settleMacWrite,
  parseDetailsAnswer,
  macWriteInFlight,
  macWriteOffers,
  MERGE_QUEUE_WRITE_OPERATIONS,
  parseMergeQueueAnswer,
  setMergeQueueFlagForTests,
  setPageMergeQueueOperationsForTests,
  confirmQueueChangeChecked,
  draftOwner,
  markQueueChangeUnconfirmed,
  unconfirmedQueueChange,
  holdPullRequestStateChanges,
  LIFECYCLE_WRITE_OPERATIONS,
  macLifecycleWriteOffers,
  parsePullRequestStateAnswer,
  parsePullRequestDraftAnswer,
  parseAutoMergeEnableAnswer,
  registerUnconfirmedStateChanges,
  unconfirmedStateChangesChanged,
  setLifecycleFlagForTests,
  setPageLifecycleOperationsForTests,
  COMMENT_EDIT_WRITE_OPERATIONS,
  finishMacWrite,
  macCommentEditOffers,
  parseCommentDeleteAnswer,
  parseCommentEditAnswer,
  parseCommentStateAnswer,
  setCommentEditsFlagForTests,
  setPageCommentEditOperationsForTests,
  macPendingReviewOffers,
  PENDING_REVIEW_READ,
  PENDING_REVIEW_WRITE_OPERATIONS,
  parseDraftAddAnswer,
  parseReviewDiscardAnswer,
  REVIEW_WRITE_OPERATIONS,
  setPagePendingReviewOperationsForTests,
  setPendingReviewsFlagForTests,
  setReviewsFlagForTests,
} from "./desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  PANEL_READ_OPERATIONS,
  macReadTicket,
  readOnSessionMachine,
  resetDesktopReadPathForTests,
} from "./desktopReadPath";
import {
  acceptMacRead,
  advanceMacReadScope,
  beginMacRead,
  macReadEpoch,
  macReadScopeKey,
} from "./macReadScope";
import { desktopCodeHostWrite } from "./nativeBridge";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const GITHUB_PR = "https://github.com/acme/app/pull/42";
const SPOKE_PR = "https://2.spoke.example.com/example-org/universe/pull/20002346";
const FULL_WRITES = { version: 1, operations: [...GITHUB_WRITE_SET_V1] };
const COMMENT = { author: "mac-octo", body: "hi", created_at: "2026-11-05T10:00:00Z", url: "u" };
const REVIEW_COMMENT = {
  id: "IC_1",
  author: "mac-octo",
  body: "hi",
  created_at: null,
  url: null,
  minimized: false,
};
const THREAD = {
  id: "PRRT_1",
  path: "src/app.js",
  side: "RIGHT",
  line: 12,
  start_line: null,
  start_side: null,
  original_line: 12,
  original_start_line: null,
  subject_type: "LINE",
  resolved: false,
  outdated: false,
  can_reply: true,
  diff_hunk: "@@ -1 +1 @@",
  comments: { data: [REVIEW_COMMENT], has_more: false, next_cursor: null },
};

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

/** A shell that reads GitHub as the Mac's account and advertises `writes`. */
async function readyShell(
  options: {
    writes?: unknown;
    codeHostWrite?: unknown;
    codeHostWriteCancel?: unknown;
    featureFlags?: unknown;
  } = {},
) {
  const writes = "writes" in options ? options.writes : FULL_WRITES;
  const codeHostWrite = "codeHostWrite" in options ? options.codeHostWrite : vi.fn();
  const codeHostWriteCancel =
    "codeHostWriteCancel" in options ? options.codeHostWriteCancel : vi.fn();
  installShell({
    codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
    ...(codeHostWrite === undefined ? {} : { codeHostWrite }),
    ...(codeHostWriteCancel === undefined ? {} : { codeHostWriteCancel }),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        { id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true, writes },
      ],
    }),
  });
  githubMacReadsPreference.write("on");
  await desktopReadTarget(GITHUB_PR);
  return codeHostWrite;
}

/** The panel has read `host` as `login`, so the store knows the account in use. */
function readAs(login: string, generation = 1, host = "github.com") {
  expect(
    acceptMacAnswer(macReadTicket("github", host), { identity: { host, login, generation } }),
  ).toBe("accepted");
}

/** Everything (a)-(e) asks for, with this page pretending to implement the whole set. */
async function routable() {
  await readyShell();
  readAs("mac-octo");
  setMacWritesReleaseFlagForTests(true);
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
}

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  // A late callback from the test before may have watched the listeners just cleared.
  resetDesktopPullRequestWritesForTests();
});

afterEach(() => {
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  installShell(undefined);
  githubMacReadsPreference.write("automatic");
  writeMacWritesIntent("automatic");
});

describe("the write set and its two halves", () => {
  it("names every write the panel makes and the reads its controls need", () => {
    expect(GITHUB_WRITE_SET_V1).toHaveLength(11);
    expect(new Set(GITHUB_WRITE_SET_V1).size).toBe(11);
  });

  it("this page implements the whole set; an older page's three text writes aren't complete", () => {
    expect([...PAGE_WRITE_OPERATIONS]).toEqual([...GITHUB_WRITE_SET_V1]);
    expect(pageWritesComplete()).toBe(true);
    expect(
      pageWritesComplete([
        "pull_request_comment",
        "pull_request_review_reply",
        "pull_request_review_thread_create",
      ]),
    ).toBe(false);
  });

  it.each([
    ["no writes", undefined, false],
    ["an unknown version", { version: 2, operations: [...GITHUB_WRITE_SET_V1] }, false],
    ["one member missing", { version: 1, operations: GITHUB_WRITE_SET_V1.slice(1) }, false],
    ["the whole set", FULL_WRITES, true],
    ["the whole set and more", { version: 1, operations: [...GITHUB_WRITE_SET_V1, "x"] }, true],
  ])("a shell advertising %s is complete: %s", (_label, writes, expected) => {
    expect(shellWritesComplete({ id: "github", operations: [], accounts: true, writes })).toBe(
      expected,
    );
  });
});

describe("the setting", () => {
  it.each([
    ["automatic", true, true],
    ["automatic", false, false],
    ["off", true, false],
    ["off", false, false],
  ] as const)("%s with the release flag %s writes on this Mac: %s", (intent, flag, expected) => {
    expect(macWritesEnabled(intent, flag)).toBe(expected);
  });

  it("stores only Off, so Automatic follows a later policy, and reads anything else as Automatic", () => {
    expect(readMacWritesIntent()).toBe("automatic");
    writeMacWritesIntent("off");
    expect(window.localStorage.getItem("omnigent:desktop-code-host-writes")).toBe("off");
    expect(readMacWritesIntent()).toBe("off");
    window.localStorage.setItem("omnigent:desktop-code-host-writes", "on");
    expect(readMacWritesIntent()).toBe("automatic");
    writeMacWritesIntent("automatic");
    expect(window.localStorage.getItem("omnigent:desktop-code-host-writes")).toBeNull();
  });
});

describe("macWriteRoute", () => {
  it("routes to this Mac as the account the panel shows once (a)-(e) all hold", async () => {
    await routable();
    expect(macWriteRoute(GITHUB_PR, null)).toMatchObject({
      target: { provider: "github", host: "github.com", repository: "acme/app", number: 42 },
      actor: { login: "mac-octo", generation: 1 },
    });
  });

  it("goes to this Mac with this page's real write set, a complete shell and the flag on", async () => {
    await readyShell();
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    expect(macWriteRoute(GITHUB_PR, null)).toMatchObject({ actor: { login: "mac-octo" } });
  });

  it("keeps the host for an older page that implements only the text writes", async () => {
    await readyShell();
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests([
      "pull_request_comment",
      "pull_request_review_reply",
      "pull_request_review_thread_create",
    ]);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });

  it("keeps the host when the user chose Off, and follows the flag on Automatic", async () => {
    await readyShell();
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    writeMacWritesIntent("off");
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    writeMacWritesIntent("automatic");
    expect(macWriteRoute(GITHUB_PR, null)).not.toBeNull();
    setMacWritesReleaseFlagForTests(false);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });

  it("(a) keeps the host for a PR read on the session's machine, or a GitSpoke PR", async () => {
    await routable();
    expect(macWriteRoute(SPOKE_PR, null)).toBeNull();
    readOnSessionMachine(GITHUB_PR);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });

  it("(b) keeps the host when the shell lacks a member or advertises another version", async () => {
    await readyShell({ writes: { version: 1, operations: GITHUB_WRITE_SET_V1.slice(0, 3) } });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });

  it("(b) keeps the host on a shell that advertises no writes", async () => {
    await readyShell({ writes: null });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });

  it("(c) follows the release flag and the setting live", async () => {
    await routable();
    setMacWritesReleaseFlagForTests(false);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    setMacWritesReleaseFlagForTests(true);
    expect(macWriteRoute(GITHUB_PR, null)).not.toBeNull();
    writeMacWritesIntent("off");
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    writeMacWritesIntent("automatic");
    expect(macWriteRoute(GITHUB_PR, null)).not.toBeNull();
  });

  it("(d) keeps the host until an account is resolved for the host", async () => {
    await readyShell();
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    readAs("mac-octo");
    expect(macWriteRoute(GITHUB_PR, null)).not.toBeNull();
  });

  it("(b) keeps the host when a shell advertises every write but has no write bridge", async () => {
    await readyShell({ codeHostWrite: undefined });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    (
      window as unknown as { omnigentDesktop: Record<string, unknown> }
    ).omnigentDesktop.codeHostWrite = "not a function";
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });

  it("keeps the host in a browser, with no shell at all", () => {
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });
});

describe("macWriteUnavailable", () => {
  const reason = (url = GITHUB_PR) => macWriteUnavailable(url, null)?.reason ?? null;

  it("says nothing once the route resolves, and agrees with macWriteRoute", async () => {
    await routable();
    expect(macWriteRoute(GITHUB_PR, null)).not.toBeNull();
    expect(macWriteUnavailable(GITHUB_PR, null)).toBeNull();
  });

  it("says nothing for a PR the session's machine reads, or in a browser", async () => {
    expect(reason()).toBeNull();
    await routable();
    readOnSessionMachine(GITHUB_PR);
    expect(reason()).toBeNull();
  });

  it("names the release flag before the shell, which offers nothing while it's off", async () => {
    await readyShell({ writes: null });
    readAs("mac-octo");
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(reason()).toBe("release_off");
    setMacWritesReleaseFlagForTests(true);
    expect(reason()).toBe("shell_writes_unavailable");
  });

  it("names Off in Settings, and clears when the user turns it back to Automatic", async () => {
    await routable();
    writeMacWritesIntent("off");
    expect(reason()).toBe("writes_off");
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    writeMacWritesIntent("automatic");
    expect(reason()).toBeNull();
  });

  it("tells an older shell without the write bridge from one that doesn't offer the set", async () => {
    await readyShell({ codeHostWrite: undefined });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(reason()).toBe("shell_too_old");
    resetDesktopReadPathForTests();
    await readyShell({ writes: { version: 1, operations: GITHUB_WRITE_SET_V1.slice(0, 3) } });
    readAs("mac-octo");
    expect(reason()).toBe("shell_writes_unavailable");
  });

  it("names a page that doesn't implement the set", async () => {
    await routable();
    setPageWriteOperationsForTests(["pull_request_comment"]);
    expect(reason()).toBe("page_unsupported");
  });

  it("names a missing account for the PR's own host, not another host's", async () => {
    await readyShell();
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    // Signed in to an Enterprise host only: github.com still has no account.
    readAs("mac-octo", 1, "ghe.example.com");
    expect(macWriteUnavailable(GITHUB_PR, null)).toMatchObject({
      reason: "no_account",
      target: { provider: "github", host: "github.com" },
    });
    readAs("mac-octo");
    expect(reason()).toBeNull();
  });

  it("says the account is being checked after Check Again, until main answers who it is", async () => {
    await routable();
    expect(reason()).toBeNull();
    recheckMacWrites(GITHUB_PR, null);
    // The account was dropped to be read again: not yet a reason to sign in.
    expect(reason()).toBe("account_checking");
    // This shell answers with no account in use, so a sign-in is what's needed.
    await vi.waitFor(() => expect(reason()).toBe("no_account"));
  });

  it("names a GitSpoke PR read on this Mac, which can't be changed here yet", async () => {
    installShell({
      codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] }],
      }),
    });
    await desktopReadTarget(SPOKE_PR);
    expect(macWriteUnavailable(SPOKE_PR, null)).toMatchObject({
      reason: "provider_unsupported",
      target: { provider: "gitspoke" },
    });
  });
});

describe("macCouldWriteOnceReadHere", () => {
  it("is true for a GitHub PR sent to the session's machine that this Mac could change", async () => {
    await routable();
    readOnSessionMachine(GITHUB_PR);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    expect(macCouldWriteOnceReadHere(GITHUB_PR, null)).toBe(true);
  });

  it("waits on nothing but the account, which only a read here resolves", async () => {
    await readyShell();
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    readOnSessionMachine(GITHUB_PR);
    expect(macCouldWriteOnceReadHere(GITHUB_PR, null)).toBe(true);
  });

  it("is false where reading here wouldn't make posting work", async () => {
    await routable();
    readOnSessionMachine(GITHUB_PR);
    setMacWritesReleaseFlagForTests(false);
    expect(macCouldWriteOnceReadHere(GITHUB_PR, null)).toBe(false);
    setMacWritesReleaseFlagForTests(true);
    writeMacWritesIntent("off");
    expect(macCouldWriteOnceReadHere(GITHUB_PR, null)).toBe(false);
    writeMacWritesIntent("automatic");
    resetDesktopReadPathForTests();
    // An older shell reads GitHub here but has no write bridge.
    await readyShell({ codeHostWrite: undefined });
    readOnSessionMachine(GITHUB_PR);
    expect(macCouldWriteOnceReadHere(GITHUB_PR, null)).toBe(false);
  });

  it("is false for a GitSpoke PR, which this Mac reads but can't change", async () => {
    installShell({
      codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] }],
      }),
    });
    await desktopReadTarget(SPOKE_PR);
    setMacWritesReleaseFlagForTests(true);
    readOnSessionMachine(SPOKE_PR);
    expect(macCouldWriteOnceReadHere(SPOKE_PR, null)).toBe(false);
  });
});

describe("macWriteMayPatch", () => {
  it("lets an answer written as the actor update caches when nothing moved", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    expect(macWriteMayPatch(ticket, "mac-octo")).toBe(true);
    expect(macWriteMayPatch(ticket, "MAC-OCTO")).toBe(true);
  });

  it("refuses an answer written as someone other than the actor", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    expect(macWriteMayPatch(ticket, "someone-else")).toBe(false);
  });

  it("refuses after the workspace user changed", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    currentUser.id = "user-2";
    expect(macWriteMayPatch(ticket, "mac-octo")).toBe(false);
  });

  it("refuses after the PR moved to the session's machine", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    readOnSessionMachine(GITHUB_PR);
    expect(macWriteMayPatch(ticket, "mac-octo")).toBe(false);
  });

  it("refuses after a switch from A to B and back to A, though A reads again", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const scope = macReadScopeKey("github", "github.com");
    expect(acceptMacRead(beginMacRead(scope), "mac-other")).toBe("transition");
    expect(acceptMacRead(beginMacRead(scope), "mac-octo")).toBe("transition");
    expect(macWriteMayPatch(ticket, "mac-octo")).toBe(false);
  });

  it("refuses after the shell said the account changed", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    advanceMacReadScope(macReadScopeKey("github", "github.com"));
    expect(macWriteMayPatch(ticket, "mac-octo")).toBe(false);
  });
});

describe("sendMacWrite and the bridge", () => {
  const params = { repository: "acme/app", number: 42, body: "hi" };

  it("sends the request id, actor, operation and params before it returns, and answers who wrote", async () => {
    const codeHostWrite = vi.fn(async () => ({
      ok: true,
      data: { comment: COMMENT },
      actor: { login: "mac-octo" },
    }));
    await readyShell({ codeHostWrite });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const answer = sendMacWrite(ticket, "pull_request_comment", params, parseCommentAnswer);
    // Sent in the caller's task, before anything is awaited.
    expect(codeHostWrite).toHaveBeenCalledExactlyOnceWith({
      requestId: ticket.requestId,
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_comment",
      params,
    });
    expect(ticket.requestId).toMatch(/^[A-Za-z0-9_-]{1,128}$/);
    expect(await answer).toEqual({ data: { comment: COMMENT }, writer: "mac-octo" });
  });

  it("throws main's code and copy for a refusal", async () => {
    await readyShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "account_changed", message: "The GitHub account on this Mac changed." },
      }),
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const error: unknown = await sendMacWrite(
      ticket,
      "pull_request_comment",
      {},
      parseCommentAnswer,
    ).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(MacWriteError);
    expect((error as MacWriteError).code).toBe("account_changed");
    expect((error as MacWriteError).outcomeUnknown).toBe(false);
  });

  it("keeps writes on the host where the shell can write but not cancel", async () => {
    await readyShell({ codeHostWriteCancel: undefined });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });
});

describe("a change of workspace user", () => {
  /** A routable shell whose write the test answers when it likes. */
  async function heldShell() {
    let answer: (value: unknown) => void = () => {};
    const codeHostWrite = vi.fn(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const codeHostWriteCancel = vi.fn();
    await readyShell({ codeHostWrite, codeHostWriteCancel });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const done = { ok: true, data: { comment: COMMENT }, actor: { login: "mac-octo" } };
    return { codeHostWrite, codeHostWriteCancel, answer: () => answer(done) };
  }

  it.each([
    ["A to B", ["user-2"]],
    ["A to B and back to A", ["user-2", "user-1"]],
    ["A to signed out", [null]],
  ] as const)("%s cancels a held write and drops its answer", async (_label, users) => {
    const shell = await heldShell();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const abandon = vi.fn();
    const pending = sendMacWrite(
      ticket,
      "pull_request_comment",
      { repository: "acme/app", number: 42, body: "hi" },
      parseCommentAnswer,
      abandon,
    );
    for (const user of users) signInAs(user);
    expect(shell.codeHostWriteCancel).toHaveBeenCalledExactlyOnceWith(ticket.requestId);
    expect(abandon).toHaveBeenCalledOnce();
    expect(macWriteTicketCurrent(ticket)).toBe(false);
    shell.answer();
    expect(await pending).toBe(MAC_WRITE_SUPPRESSED);
  });

  it("sends nothing for a ticket taken before the user changed", async () => {
    const shell = await heldShell();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    signInAs("user-2");
    signInAs("user-1");
    const answer = await sendMacWrite(ticket, "pull_request_comment", {}, parseCommentAnswer);
    expect(answer).toBe(MAC_WRITE_SUPPRESSED);
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
  });

  it("does nothing when the identity check finds the same user again", async () => {
    const shell = await heldShell();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const generation = currentWorkspaceAuthGeneration();
    const pending = sendMacWrite(ticket, "pull_request_comment", {}, parseCommentAnswer);
    signInAs("user-1");
    expect(currentWorkspaceAuthGeneration()).toBe(generation);
    expect(shell.codeHostWriteCancel).not.toHaveBeenCalled();
    shell.answer();
    expect(await pending).toEqual({ data: { comment: COMMENT }, writer: "mac-octo" });
  });

  it("still answers the original user when only the Mac's account moved", async () => {
    const shell = await heldShell();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const pending = sendMacWrite(ticket, "pull_request_comment", {}, parseCommentAnswer);
    advanceMacReadScope(macReadScopeKey("github", "github.com"));
    expect(shell.codeHostWriteCancel).not.toHaveBeenCalled();
    shell.answer();
    expect(await pending).toEqual({ data: { comment: COMMENT }, writer: "mac-octo" });
    // It may not patch caches under the new reader, but the user hears the outcome.
    expect(macWriteMayPatch(ticket, "mac-octo")).toBe(false);
  });
});

describe("settling a Mac write", () => {
  async function routableWith(codeHostWrite: () => Promise<unknown>) {
    const codeHostWriteCancel = vi.fn();
    await readyShell({ codeHostWrite, codeHostWriteCancel });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    return codeHostWriteCancel;
  }
  const done = { ok: true, data: { comment: COMMENT }, actor: { login: "mac-octo" } };

  it("still retires a write whose answer arrived but is being applied when the user changes", async () => {
    const cancel = await routableWith(async () => done);
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const abandon = vi.fn();
    const sent = sendMacWrite(ticket, "pull_request_comment", {}, parseCommentAnswer, abandon);
    let release: () => void = () => {};
    const applying = new Promise<void>((resolve) => {
      release = resolve;
    });
    const applied = vi.fn(() => applying);
    const settled = settleMacWrite(ticket, sent, { applied, failed: vi.fn() });
    await vi.waitFor(() => expect(applied).toHaveBeenCalledOnce());
    signInAs("user-2");
    expect(abandon).toHaveBeenCalledOnce();
    expect(cancel).toHaveBeenCalledExactlyOnceWith(ticket.requestId);
    release();
    await settled;
    // Applied and done: a later change finds nothing of it to retire.
    signInAs("user-3");
    expect(abandon).toHaveBeenCalledOnce();
  });

  it("applies nothing when the user changes after the answer was checked", async () => {
    await routableWith(async () => done);
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const applied = vi.fn();
    // An answer already checked as current, then a change before it's applied.
    const answered = Promise.resolve({ data: { comment: COMMENT }, writer: "mac-octo" });
    const settled = settleMacWrite(ticket, answered, { applied, failed: vi.fn() });
    signInAs("user-2");
    await settled;
    expect(applied).not.toHaveBeenCalled();
  });

  it("hands a refusal to `failed` only while the user is the click's", async () => {
    await routableWith(async () => ({ ok: false, error: { code: "rejected", message: "No." } }));
    const current = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const failed = vi.fn();
    await settleMacWrite(
      current,
      sendMacWrite(current, "pull_request_comment", {}, parseCommentAnswer),
      {
        applied: vi.fn(),
        failed,
      },
    );
    expect(failed).toHaveBeenCalledOnce();
    expect(failed.mock.calls[0][0]).toBeInstanceOf(MacWriteError);
  });

  it("applies nothing for an answer dropped by a change of user", async () => {
    let answer: (value: unknown) => void = () => {};
    await routableWith(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const handlers = { applied: vi.fn(), failed: vi.fn() };
    const settled = settleMacWrite(
      ticket,
      sendMacWrite(ticket, "pull_request_comment", {}, parseCommentAnswer),
      handlers,
    );
    signInAs("user-2");
    answer(done);
    await settled;
    expect(handlers.applied).not.toHaveBeenCalled();
    expect(handlers.failed).not.toHaveBeenCalled();
  });
});

describe("a title or description that changed on GitHub", () => {
  it("carries the value GitHub has now on the error, from a well-formed answer only", async () => {
    await readyShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "pull_request_changed", message: "This title changed on GitHub." },
        current: { field: "title", value: "Theirs" },
      }),
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const error = (await sendMacWrite(
      ticket,
      "pull_request_details_update",
      {},
      parseDetailsAnswer,
    ).catch((caught: unknown) => caught)) as MacWriteError;
    expect(error.code).toBe("pull_request_changed");
    expect(error.current).toEqual({ field: "title", value: "Theirs" });
  });

  it.each([
    ["another field", { field: "state", value: "open" }],
    ["a value that isn't text", { field: "title", value: 7 }],
  ])("drops a current value naming %s", async (_label, current) => {
    installShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "pull_request_changed", message: "Changed." },
        current,
      }),
      codeHostWriteCancel: vi.fn(),
    });
    const answer = await desktopCodeHostWrite({
      requestId: "req-1",
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_details_update",
      params: {},
    });
    expect(answer).toEqual({
      ok: false,
      error: { code: "pull_request_changed", message: "Changed." },
    });
  });

  it("reads a details answer only with both fields as text", () => {
    expect(parseDetailsAnswer({ title: "T", body: "" })).toEqual({ title: "T", body: "" });
    expect(parseDetailsAnswer({ title: "T" })).toBeNull();
    expect(parseDetailsAnswer({ title: 1, body: "" })).toBeNull();
  });
});

describe("the bridge's answers", () => {
  const request = {
    requestId: "req-1",
    provider: "github" as const,
    host: "github.com",
    actor: { login: "mac-octo", generation: 1 },
    operation: "pull_request_comment",
    params: {},
  };

  it("answers unsupported without sending on a shell without the bridge", async () => {
    installShell({});
    expect(await desktopCodeHostWrite(request)).toMatchObject({
      ok: false,
      error: { code: "unsupported" },
    });
  });

  it.each([
    ["a rejected call", () => Promise.reject(new Error("IPC lost"))],
    ["a success that names no writer", async () => ({ ok: true, data: {} })],
    ["an answer of another shape", async () => "done"],
    ["a refusal without copy", async () => ({ ok: false, error: { code: "rejected" } })],
  ])("answers outcome_unknown for %s once sent", async (_label, codeHostWrite) => {
    installShell({ codeHostWrite });
    const answer = await desktopCodeHostWrite(request);
    expect(answer).toMatchObject({ ok: false, error: { code: "outcome_unknown" } });
    if (!answer.ok) expect(answer.error.message).toMatch(/may have made this change/);
  });
});

describe("the release flag, live", () => {
  it("asks the shell again when the flag turns on and off, so its advertised writes follow", async () => {
    let advertised = false;
    let deliver: ((value: unknown) => void) | null = null;
    installShell({
      codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
      codeHostWrite: vi.fn(),
      codeHostWriteCancel: vi.fn(),
      codeHostAccounts: {
        get: async () => ({
          ok: true,
          data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
        }),
        set: async () => ({ ok: false }),
        onChanged: () => () => {},
      },
      // Main advertises writes only while the flag is on.
      getDesktopFeatures: async () => ({
        codeHosts: [
          {
            id: "github",
            operations: [...GITHUB_MAC_READ_OPERATIONS],
            accounts: true,
            ...(advertised ? { writes: FULL_WRITES } : {}),
          },
        ],
        featureFlags: { names: ["desktop-code-host-writes"] },
      }),
      featureFlags: {
        subscribe: (_name: string, _options: unknown, callback: (value: unknown) => void) => {
          deliver = callback;
          return () => {};
        },
      },
    });
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    readAs("mac-octo");
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    // Off at startup: the first snapshot has no writes.
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    await vi.waitFor(() => expect(deliver).not.toBeNull());

    advertised = true;
    deliver!(true);
    await vi.waitFor(() => expect(macWriteRoute(GITHUB_PR, null)).not.toBeNull());

    advertised = false;
    deliver!(false);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    // The flag on again without the shell advertising writes still keeps the host.
    deliver!(true);
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
  });
});

describe("what a Mac write's answer must look like", () => {
  it.each([
    ["a whole comment", COMMENT, true],
    ["a comment with null author and time", { ...COMMENT, author: null, created_at: null }, true],
    ["no link", { ...COMMENT, url: null }, false],
    ["a body that isn't text", { ...COMMENT, body: 3 }, false],
    ["a time that isn't text", { ...COMMENT, created_at: 5 }, false],
    ["no time field", { author: "a", body: "b", url: "u" }, false],
    ["an author id that isn't text", { ...COMMENT, author_id: 7 }, false],
  ])("a comment answer with %s is accepted: %s", (_label, comment, expected) => {
    expect(parseCommentAnswer({ comment }) !== null).toBe(expected);
  });

  it("refuses a comment answer with no data or no comment", () => {
    expect(parseCommentAnswer(undefined)).toBeNull();
    expect(parseCommentAnswer({})).toBeNull();
  });

  it.each([
    ["a whole review comment", REVIEW_COMMENT, true],
    ["no minimized flag", { ...REVIEW_COMMENT, minimized: undefined }, true],
    ["no id", { ...REVIEW_COMMENT, id: undefined }, false],
    ["an empty id", { ...REVIEW_COMMENT, id: "" }, false],
    ["a time that isn't text", { ...REVIEW_COMMENT, created_at: 5 }, false],
    ["a minimized flag that isn't a boolean", { ...REVIEW_COMMENT, minimized: "no" }, false],
  ])("a reply answer with %s is accepted: %s", (_label, comment, expected) => {
    expect(parseReplyAnswer({ comment }) !== null).toBe(expected);
  });

  it.each([
    ["a whole thread", THREAD, true],
    ["a file thread with no lines", { ...THREAD, subject_type: "FILE", line: null }, true],
    ["a thread without optional fields", { ...THREAD, start_side: undefined }, true],
    [
      "a malformed comment inside",
      { ...THREAD, comments: { ...THREAD.comments, data: [null] } },
      false,
    ],
    [
      "a comment inside without an id",
      { ...THREAD, comments: { ...THREAD.comments, data: [{ ...REVIEW_COMMENT, id: undefined }] } },
      false,
    ],
    ["no comments", { ...THREAD, comments: { ...THREAD.comments, data: [] } }, false],
    ["a side GitHub doesn't have", { ...THREAD, side: "BOTH" }, false],
    ["a line that isn't a number", { ...THREAD, line: "12" }, false],
    ["no path", { ...THREAD, path: undefined }, false],
    ["no diff hunk", { ...THREAD, diff_hunk: undefined }, false],
    ["a resolved flag that isn't a boolean", { ...THREAD, resolved: "no" }, false],
    ["no paging fields", { ...THREAD, comments: { data: [REVIEW_COMMENT] } }, false],
    ["locally pending ids", { ...THREAD, pending_comment_ids: ["x"] }, false],
  ])("a new-conversation answer with %s is accepted: %s", (_label, thread, expected) => {
    expect(parseThreadAnswer({ thread }) !== null).toBe(expected);
  });

  it("accepts an explicit null thread, and refuses no thread field or no data", () => {
    expect(parseThreadAnswer({ thread: null })).toEqual({ thread: null });
    expect(parseThreadAnswer({})).toBeNull();
    expect(parseThreadAnswer(undefined)).toBeNull();
  });
});

describe("which cached reads a Mac write may touch", () => {
  it("only its PR's queries of its session under its own scope", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    const scope = macReadScopeKey("github", "github.com");
    const otherUser = macReadScopeKey("github", "github.com", "user-2");
    expect(
      cachedUnderTicket(["github-info", "s", GITHUB_PR, ...scope], ticket, "github-info", "s"),
    ).toBe(true);
    expect(
      cachedUnderTicket(
        ["github-review-threads", "s", GITHUB_PR, "acct", "rev", ...scope],
        ticket,
        "github-review-threads",
        "s",
      ),
    ).toBe(true);
    for (const key of [
      ["github-info", "s", GITHUB_PR],
      ["github-info", "s", GITHUB_PR, "host-mutation-context"],
      ["github-info", "s"],
      ["github-info", "s", GITHUB_PR, ...otherUser],
      ["github-info", "other-session", GITHUB_PR, ...scope],
      ["github-info", "s", "https://github.com/acme/app/pull/43", ...scope],
      ["github-review-threads", "s", GITHUB_PR, ...scope],
    ]) {
      expect(cachedUnderTicket(key, ticket, "github-info", "s"), JSON.stringify(key)).toBe(false);
    }
  });

  it("a PR's shared record only as the account it acts as, in the scope's epoch now", async () => {
    await routable();
    const route = macWriteRoute(GITHUB_PR, null)!;
    const writer = route.actor.login;
    const ticket = beginMacWrite(route);
    const scope = macReadScopeKey("github", "github.com");
    const record = (reader: string | null, epoch: number, root = "github-info") => [
      root,
      "pr",
      GITHUB_PR,
      ...scope,
      "reader",
      reader,
      epoch,
    ];
    const epoch = macReadEpoch(scope);
    // Any session's view of the record shows the write.
    for (const session of ["s", "other-session"]) {
      expect(cachedUnderTicket(record(writer, epoch), ticket, "github-info", session)).toBe(true);
    }
    expect(cachedUnderTicket(record(writer.toUpperCase(), epoch), ticket, "github-info", "s")).toBe(
      true,
    );
    for (const key of [
      record("someone-else", epoch),
      record(null, epoch),
      record(writer, epoch + 1),
      record(writer, epoch, "github-review-threads"),
      [
        "github-info",
        "pr",
        GITHUB_PR,
        ...macReadScopeKey("github", "github.com", "user-2"),
        "reader",
        writer,
        epoch,
      ],
    ]) {
      expect(cachedUnderTicket(key, ticket, "github-info", "s"), JSON.stringify(key)).toBe(false);
    }
    // After a move of the scope, nothing from before the click is patched, not even the new epoch's.
    advanceMacReadScope(scope);
    const moved = macReadEpoch(scope);
    expect(cachedUnderTicket(record(writer, moved), ticket, "github-info", "s")).toBe(false);
  });

  it("says what was done and as whom when the panel can't show it", async () => {
    await routable();
    const ticket = beginMacWrite(macWriteRoute(GITHUB_PR, null)!);
    expect(macWriteDoneMessage("pull_request_comment", ticket, "mac-octo")).toBe(
      "Commented on #42 as @mac-octo.",
    );
    expect(macWriteDoneMessage("pull_request_review_reply", ticket, "mac-octo")).toBe(
      "Replied on #42 as @mac-octo.",
    );
    expect(macWriteDoneMessage("pull_request_review_thread_create", ticket, "mac-octo")).toBe(
      "Started a conversation on #42 as @mac-octo.",
    );
    expect(macWriteDoneMessage("pull_request_convert_to_draft", ticket, "mac-octo")).toBe(
      "Converted #42 to a draft as @mac-octo.",
    );
    expect(macWriteDoneMessage("pull_request_ready_for_review", ticket, "mac-octo")).toBe(
      "Marked #42 ready for review as @mac-octo.",
    );
    expect(macWriteDoneMessage("pull_request_auto_merge_enable", ticket, "mac-octo")).toBe(
      "Turned on auto-merge for #42 as @mac-octo.",
    );
  });
});

describe("merge queue changes", () => {
  const QUEUE_WRITES = {
    version: 1,
    operations: [...GITHUB_WRITE_SET_V1, ...MERGE_QUEUE_WRITE_OPERATIONS],
  };
  /** The whole route, with the shell listing `operations` among its writes. */
  async function queueRoutable(operations: readonly string[] = MERGE_QUEUE_WRITE_OPERATIONS) {
    await readyShell({
      writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...operations] },
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setMergeQueueFlagForTests(true);
    return macWriteRoute(GITHUB_PR, null);
  }

  it("offers each queue change where the route, the page, the shell and the queue's flag all have it", async () => {
    const route = await queueRoutable();
    expect(route).not.toBeNull();
    expect(macWriteOffers(route, "pull_request_merge_queue_add")).toBe(true);
    expect(macWriteOffers(route, "pull_request_merge_queue_remove")).toBe(true);
    expect(macWriteOffers(null, "pull_request_merge_queue_add")).toBe(false);
  });

  it.each([
    [["pull_request_merge_queue_add"], { add: true, remove: false }],
    [["pull_request_merge_queue_remove"], { add: false, remove: true }],
    [[], { add: false, remove: false }],
  ])("offers only what a shell listing %j offers", async (operations, expected) => {
    const route = await queueRoutable(operations);
    expect({
      add: macWriteOffers(route, "pull_request_merge_queue_add"),
      remove: macWriteOffers(route, "pull_request_merge_queue_remove"),
    }).toEqual(expected);
  });

  it("offers nothing while the queue's flag is off or unanswered, though the shell still lists them", async () => {
    const route = await queueRoutable();
    setMergeQueueFlagForTests(false);
    expect(macWriteOffers(route, "pull_request_merge_queue_add")).toBe(false);
    // Unanswered: the flag starts off, whatever the shell lists.
    setMergeQueueFlagForTests(null);
    expect(macWriteOffers(route, "pull_request_merge_queue_remove")).toBe(false);
  });

  it("offers nothing this page doesn't implement", async () => {
    const route = await queueRoutable();
    setPageMergeQueueOperationsForTests(["pull_request_merge_queue_remove"]);
    expect(macWriteOffers(route, "pull_request_merge_queue_add")).toBe(false);
    expect(macWriteOffers(route, "pull_request_merge_queue_remove")).toBe(true);
  });

  it("keeps the whole set's route for an older shell, which offers no queue changes", async () => {
    const route = await queueRoutable([]);
    expect(route).not.toBeNull();
    expect(shellWritesComplete({ id: "github", operations: [], writes: QUEUE_WRITES })).toBe(true);
    expect(macWriteOffers(route, "pull_request_merge_queue_add")).toBe(false);
  });

  it("holds merge, auto-merge and queue changes to one pull request as one, from any of them", async () => {
    const codeHostWrite = vi.fn(() => new Promise(() => {}));
    await readyShell({ codeHostWrite, writes: QUEUE_WRITES });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const route = macWriteRoute(GITHUB_PR, null)!;
    const target = route.target;
    void sendMacWrite(
      beginMacWrite(route),
      "pull_request_merge_queue_add",
      {},
      parseMergeQueueAnswer,
    );
    for (const operation of [
      "pull_request_merge_queue_add",
      "pull_request_merge_queue_remove",
      "pull_request_merge",
      "pull_request_auto_merge_disable",
    ] as const) {
      expect(macWriteInFlight(operation, target)).toBe(true);
    }
    // Other changes to the same pull request don't wait for it.
    expect(macWriteInFlight("pull_request_labels_update", target)).toBe(false);
    expect(macWriteInFlight("pull_request_comment", target)).toBe(false);
  });

  it("holds the queue until the shell answers, across a change of workspace user", async () => {
    let answer!: (value: unknown) => void;
    const codeHostWrite = vi.fn(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    await readyShell({ codeHostWrite, writes: QUEUE_WRITES });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const route = macWriteRoute(GITHUB_PR, null)!;
    const pending = sendMacWrite(
      beginMacWrite(route),
      "pull_request_merge_queue_remove",
      {},
      parseMergeQueueAnswer,
    ).catch(() => null);
    // Another user, then back: main still runs the write, so the page still holds it.
    signInAs("user-2");
    signInAs("user-1");
    expect(macWriteInFlight("pull_request_merge_queue_add", route.target)).toBe(true);
    answer({
      ok: true,
      data: { object: "session.github.pr_merge_queue", pr_url: GITHUB_PR, queued: false },
      actor: { login: "mac-octo" },
    });
    await pending;
    await vi.waitFor(() =>
      expect(macWriteInFlight("pull_request_merge_queue_add", route.target)).toBe(false),
    );
    // Nothing is sent again by itself.
    expect(codeHostWrite).toHaveBeenCalledOnce();
  });

  it("shares one hold between spellings of the same PR, for its own workspace user only", () => {
    const shouting = "https://github.com/ACME/App/pull/42";
    const revision = markQueueChangeUnconfirmed(
      draftOwner(),
      shouting,
      "pull_request_merge_queue_add",
      { login: "mac-octo", reader: null },
    );
    expect(unconfirmedQueueChange(GITHUB_PR)?.operation).toBe("pull_request_merge_queue_add");
    // Another user has no hold of this one's.
    signInAs("user-2");
    expect(unconfirmedQueueChange(GITHUB_PR)).toBeNull();
    signInAs("user-1");
    // A stale revision clears nothing; the marker's own does, through either spelling.
    confirmQueueChangeChecked(draftOwner(), GITHUB_PR, revision - 1);
    expect(unconfirmedQueueChange(shouting)).not.toBeNull();
    confirmQueueChangeChecked(draftOwner(), GITHUB_PR, revision);
    expect(unconfirmedQueueChange(shouting)).toBeNull();
  });

  it("reads a queue change's answer only in its own shape", () => {
    expect(
      parseMergeQueueAnswer({
        object: "session.github.pr_merge_queue",
        pr_url: GITHUB_PR,
        queued: true,
      }),
    ).toEqual({ pr_url: GITHUB_PR, queued: true });
    expect(
      parseMergeQueueAnswer({ object: "session.github.pr_merge", pr_url: GITHUB_PR }),
    ).toBeNull();
    expect(
      parseMergeQueueAnswer({
        object: "session.github.pr_merge_queue",
        pr_url: GITHUB_PR,
        queued: "yes",
      }),
    ).toBeNull();
  });
});

describe("closing and reopening", () => {
  const LIFECYCLE_WRITES = {
    version: 1,
    operations: [...GITHUB_WRITE_SET_V1, ...LIFECYCLE_WRITE_OPERATIONS],
  };
  /** The whole route, with the shell listing `operations` among its writes. */
  async function lifecycleRoutable(
    operations: readonly string[] = LIFECYCLE_WRITE_OPERATIONS,
    flag: boolean | null = true,
  ) {
    await readyShell({
      writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...operations] },
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setLifecycleFlagForTests(flag);
    return macWriteRoute(GITHUB_PR, null);
  }

  it("offers each where the route, the page, the shell and the lifecycle flag all have it", async () => {
    const route = await lifecycleRoutable();
    expect(route).not.toBeNull();
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(true);
    expect(macLifecycleWriteOffers(route, "pull_request_reopen")).toBe(true);
    expect(macLifecycleWriteOffers(null, "pull_request_close")).toBe(false);
  });

  it("offers nothing while the flag is off or unanswered, though the shell still lists them", async () => {
    const route = await lifecycleRoutable(LIFECYCLE_WRITE_OPERATIONS, false);
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(false);
    setLifecycleFlagForTests(null);
    expect(macLifecycleWriteOffers(route, "pull_request_reopen")).toBe(false);
  });

  it("offers nothing to a newer page on an older shell that lists neither", async () => {
    const route = await lifecycleRoutable([]);
    // The whole set's route stays: everything else still goes to the Mac.
    expect(route).not.toBeNull();
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(false);
  });

  it("leaves an older page's route alone on a newer shell that lists them", async () => {
    expect(shellWritesComplete({ id: "github", operations: [], writes: LIFECYCLE_WRITES })).toBe(
      true,
    );
    // An older page implements none of them: the shell's extras change nothing for it.
    const route = await lifecycleRoutable();
    setPageLifecycleOperationsForTests([]);
    expect(route).not.toBeNull();
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(false);
  });

  it("follows the lifecycle flag live, asking the shell again as it turns on and off", async () => {
    let advertised = false;
    const deliver = new Map<string, (value: unknown) => void>();
    const getDesktopFeatures = vi.fn(async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          writes: advertised ? LIFECYCLE_WRITES : FULL_WRITES,
        },
      ],
      featureFlags: { names: ["desktop-code-host-lifecycle-writes"] },
    }));
    installShell({
      codeHostQuery: vi.fn(async () => ({ ok: true, data: [] })),
      codeHostWrite: vi.fn(),
      codeHostWriteCancel: vi.fn(),
      codeHostAccounts: {
        get: async () => ({
          ok: true,
          data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
        }),
        set: async () => ({ ok: false }),
        onChanged: () => () => {},
      },
      getDesktopFeatures,
      featureFlags: {
        subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
          deliver.set(name, callback);
          return () => {};
        },
      },
    });
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const route = macWriteRoute(GITHUB_PR, null);
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(false);
    await vi.waitFor(() => expect(deliver.has("desktop-code-host-lifecycle-writes")).toBe(true));
    const flag = deliver.get("desktop-code-host-lifecycle-writes")!;
    const asked = getDesktopFeatures.mock.calls.length;

    advertised = true;
    flag(true);
    await vi.waitFor(() =>
      expect(macLifecycleWriteOffers(macWriteRoute(GITHUB_PR, null), "pull_request_close")).toBe(
        true,
      ),
    );
    expect(getDesktopFeatures.mock.calls.length).toBeGreaterThan(asked);

    // Off again: hidden at once, before the shell has answered the new ask.
    flag(false);
    expect(macLifecycleWriteOffers(macWriteRoute(GITHUB_PR, null), "pull_request_close")).toBe(
      false,
    );
  });

  it("offers nothing without a write bridge", async () => {
    await readyShell({ codeHostWrite: undefined, writes: LIFECYCLE_WRITES });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setLifecycleFlagForTests(true);
    expect(macWriteRoute(GITHUB_PR, null)).toBeNull();
    expect(macLifecycleWriteOffers(macWriteRoute(GITHUB_PR, null), "pull_request_close")).toBe(
      false,
    );
  });

  it("holds closing, reopening and merging to one pull request as one", async () => {
    const codeHostWrite = vi.fn(() => new Promise(() => {}));
    await readyShell({ codeHostWrite, writes: LIFECYCLE_WRITES });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const route = macWriteRoute(GITHUB_PR, null)!;
    void sendMacWrite(beginMacWrite(route), "pull_request_close", {}, parsePullRequestStateAnswer);
    for (const operation of [
      "pull_request_close",
      "pull_request_reopen",
      "pull_request_merge",
      "pull_request_auto_merge_disable",
      "pull_request_merge_queue_add",
    ] as const) {
      expect(macWriteInFlight(operation, route.target)).toBe(true);
    }
    expect(macWriteInFlight("pull_request_comment", route.target)).toBe(false);
  });

  it("keeps state changes waiting while a close posts its comment, until it's released", async () => {
    await lifecycleRoutable();
    const target = macWriteRoute(GITHUB_PR, null)!.target;
    const release = holdPullRequestStateChanges(target);
    expect(macWriteInFlight("pull_request_merge", target)).toBe(true);
    expect(macWriteInFlight("pull_request_close", target)).toBe(true);
    // A comment isn't a state change: it posts while the hold is on.
    expect(macWriteInFlight("pull_request_comment", target)).toBe(false);
    release();
    expect(macWriteInFlight("pull_request_merge", target)).toBe(false);
    // Releasing twice changes nothing.
    release();
    expect(macWriteInFlight("pull_request_merge", target)).toBe(false);
  });

  it("reads a state answer only as one of the three states GitHub has", () => {
    const url = "https://github.com/acme/app/pull/42";
    expect(
      parsePullRequestStateAnswer({
        object: "session.github.pr_state",
        pr_url: url,
        state: "CLOSED",
      }),
    ).toEqual({ pr_url: url, state: "CLOSED" });
    expect(
      parsePullRequestStateAnswer({
        object: "session.github.pr_state",
        pr_url: url,
        state: "MERGED",
      }),
    ).toEqual({ pr_url: url, state: "MERGED" });
    for (const answer of [
      { object: "session.github.pr_state", pr_url: url, state: "closed" },
      { object: "session.github.pr_state", pr_url: url },
      { object: "session.github.pr_merge", pr_url: url, state: "CLOSED" },
      { object: "session.github.pr_state", state: "OPEN" },
      null,
    ]) {
      expect(parsePullRequestStateAnswer(answer)).toBeNull();
    }
  });
});

describe("converting to a draft, marking ready and turning on auto-merge", () => {
  const ADDED = [
    "pull_request_convert_to_draft",
    "pull_request_ready_for_review",
    "pull_request_auto_merge_enable",
  ] as const;
  async function draftRoutable(operations: readonly string[], flag: boolean | null = true) {
    await readyShell({
      writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...operations] },
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setLifecycleFlagForTests(flag);
    return macWriteRoute(GITHUB_PR, null);
  }

  it("offers each where the shell lists it and the lifecycle flag is on", async () => {
    const route = await draftRoutable(LIFECYCLE_WRITE_OPERATIONS);
    for (const operation of ADDED) expect(macLifecycleWriteOffers(route, operation)).toBe(true);
  });

  it("offers none to this page on an older shell that lists only close and reopen", async () => {
    const route = await draftRoutable(["pull_request_close", "pull_request_reopen"]);
    expect(route).not.toBeNull();
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(true);
    for (const operation of ADDED) expect(macLifecycleWriteOffers(route, operation)).toBe(false);
  });

  it("offers one the shell lists alone, and not the others", async () => {
    const route = await draftRoutable(["pull_request_ready_for_review"]);
    expect(macLifecycleWriteOffers(route, "pull_request_ready_for_review")).toBe(true);
    expect(macLifecycleWriteOffers(route, "pull_request_convert_to_draft")).toBe(false);
    expect(macLifecycleWriteOffers(route, "pull_request_auto_merge_enable")).toBe(false);
  });

  it("offers none while the lifecycle flag is off, though the shell lists them", async () => {
    const route = await draftRoutable(LIFECYCLE_WRITE_OPERATIONS, false);
    for (const operation of ADDED) expect(macLifecycleWriteOffers(route, operation)).toBe(false);
  });

  it("leaves an older page that knows only close and reopen as it was on a newer shell", async () => {
    const everything = {
      version: 1,
      operations: [...GITHUB_WRITE_SET_V1, ...LIFECYCLE_WRITE_OPERATIONS],
    };
    expect(shellWritesComplete({ id: "github", operations: [], writes: everything })).toBe(true);
    const route = await draftRoutable(LIFECYCLE_WRITE_OPERATIONS);
    setPageLifecycleOperationsForTests(["pull_request_close", "pull_request_reopen"]);
    expect(route).not.toBeNull();
    expect(macLifecycleWriteOffers(route, "pull_request_close")).toBe(true);
    for (const operation of ADDED) expect(macLifecycleWriteOffers(route, operation)).toBe(false);
  });

  it("holds every state change, not a comment, while one is in flight", async () => {
    await draftRoutable(LIFECYCLE_WRITE_OPERATIONS);
    const target = macWriteRoute(GITHUB_PR, null)!.target;
    const release = holdPullRequestStateChanges(target);
    for (const operation of ADDED) expect(macWriteInFlight(operation, target)).toBe(true);
    expect(macWriteInFlight("pull_request_merge", target)).toBe(true);
    release();
    for (const operation of ADDED) expect(macWriteInFlight(operation, target)).toBe(false);
  });

  it("holds merging and state changes for an unconfirmed change, for its user and PR only", async () => {
    await draftRoutable(LIFECYCLE_WRITE_OPERATIONS);
    const target = macWriteRoute(GITHUB_PR, null)!.target;
    let changes: { owner: string; prUrl: string }[] = [
      { owner: draftOwner(), prUrl: GITHUB_PR.replace("acme/app", "ACME/App") },
    ];
    const unregister = registerUnconfirmedStateChanges(() => changes);
    // The PR is the same whatever the URL's case.
    for (const operation of [
      "pull_request_merge",
      "pull_request_auto_merge_disable",
      "pull_request_merge_queue_add",
      "pull_request_close",
      ...ADDED,
    ] as const) {
      expect(macWriteInFlight(operation, target)).toBe(true);
    }
    expect(macWriteInFlight("pull_request_comment", target)).toBe(false);
    expect(macWriteInFlight("pull_request_merge", { ...target, number: 7 })).toBe(false);
    // Another workspace user's unconfirmed change isn't this one's to wait out.
    changes = [{ owner: "someone-else", prUrl: GITHUB_PR }];
    unconfirmedStateChangesChanged();
    expect(macWriteInFlight("pull_request_merge", target)).toBe(false);
    changes = [{ owner: draftOwner(), prUrl: GITHUB_PR }];
    expect(macWriteInFlight("pull_request_merge", target)).toBe(true);
    unregister();
    expect(macWriteInFlight("pull_request_merge", target)).toBe(false);
  });

  it("reads a draft answer and an auto-merge answer, and nothing else", () => {
    expect(
      parsePullRequestDraftAnswer({
        object: "session.github.pr_draft",
        pr_url: GITHUB_PR,
        is_draft: true,
      }),
    ).toEqual({ pr_url: GITHUB_PR, is_draft: true });
    expect(
      parsePullRequestDraftAnswer({
        object: "session.github.pr_draft",
        pr_url: GITHUB_PR,
        is_draft: "yes",
      }),
    ).toBeNull();
    expect(
      parsePullRequestDraftAnswer({
        object: "session.github.pr_state",
        pr_url: GITHUB_PR,
        is_draft: true,
      }),
    ).toBeNull();
    expect(
      parseAutoMergeEnableAnswer({
        object: "session.github.pr_auto_merge",
        pr_url: GITHUB_PR,
        enabled_at: "2026-11-08T12:00:00Z",
      }),
    ).toEqual({ pr_url: GITHUB_PR, enabled_at: "2026-11-08T12:00:00Z" });
    expect(
      parseAutoMergeEnableAnswer({ object: "session.github.pr_auto_merge", pr_url: GITHUB_PR }),
    ).toBeNull();
    expect(
      parseAutoMergeEnableAnswer({
        object: "session.github.pr_merge",
        pr_url: GITHUB_PR,
        enabled_at: "x",
      }),
    ).toBeNull();
  });
});

describe("editing and deleting comments", () => {
  async function commentEditsRoutable(
    operations: readonly string[] = COMMENT_EDIT_WRITE_OPERATIONS,
    flag: boolean | null = true,
    codeHostWrite: unknown = vi.fn(),
  ) {
    await readyShell({
      writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...operations] },
      codeHostWrite,
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setCommentEditsFlagForTests(flag);
    return macWriteRoute(GITHUB_PR, null);
  }

  it("offers each where the route, the page, the shell and their flag all have it", async () => {
    const route = await commentEditsRoutable();
    expect(macCommentEditOffers(route, "pull_request_comment_edit")).toBe(true);
    expect(macCommentEditOffers(route, "pull_request_comment_delete")).toBe(true);
    expect(macCommentEditOffers(null, "pull_request_comment_edit")).toBe(false);
  });

  it("offers nothing while their flag is off or unanswered, though the shell lists them", async () => {
    const route = await commentEditsRoutable(COMMENT_EDIT_WRITE_OPERATIONS, false);
    expect(macCommentEditOffers(route, "pull_request_comment_edit")).toBe(false);
    setCommentEditsFlagForTests(null);
    expect(macCommentEditOffers(route, "pull_request_comment_delete")).toBe(false);
  });

  it("offers only what the shell lists, and nothing a page doesn't implement", async () => {
    const route = await commentEditsRoutable(["pull_request_comment_delete"]);
    expect(macCommentEditOffers(route, "pull_request_comment_edit")).toBe(false);
    expect(macCommentEditOffers(route, "pull_request_comment_delete")).toBe(true);
    setPageCommentEditOperationsForTests([]);
    expect(macCommentEditOffers(route, "pull_request_comment_delete")).toBe(false);
  });

  it("holds an edit and a delete on the same PR as one change in flight", async () => {
    let answer!: (value: unknown) => void;
    const held = new Promise((resolve) => {
      answer = resolve;
    });
    const route = await commentEditsRoutable(COMMENT_EDIT_WRITE_OPERATIONS, true, () => held);
    const target = route!.target;
    const ticket = beginMacWrite(route!);
    const sent = sendMacWrite(ticket, "pull_request_comment_edit", {}, () => null);
    expect(macWriteInFlight("pull_request_comment_delete", target)).toBe(true);
    expect(macWriteInFlight("pull_request_comment", target)).toBe(false);
    expect(macWriteInFlight("pull_request_merge", target)).toBe(false);
    answer({ ok: false, error: { code: "cancelled", message: "x" } });
    await sent.catch(() => {});
    // Answered, then applied by its caller: the next change may go.
    finishMacWrite(ticket);
    expect(macWriteInFlight("pull_request_comment_delete", target)).toBe(false);
  });

  it("reads an edit's, a delete's and a check's answers strictly", () => {
    expect(
      parseCommentEditAnswer({
        object: "session.github.pr_comment_edit",
        comment_id: "IC_1",
        body: "b",
      }),
    ).toEqual({ comment_id: "IC_1", body: "b" });
    expect(
      parseCommentEditAnswer({ object: "session.github.pr_comment_edit", comment_id: "IC_1" }),
    ).toBeNull();
    expect(
      parseCommentDeleteAnswer({ object: "session.github.pr_comment_delete", comment_id: "IC_1" }),
    ).toEqual({ comment_id: "IC_1" });
    expect(
      parseCommentDeleteAnswer({ object: "session.github.pr_comment_edit", comment_id: "IC_1" }),
    ).toBeNull();
    const state = (fields: Record<string, unknown>) => ({
      object: "session.github.pr_comment_state",
      comment_id: "IC_1",
      ...fields,
    });
    expect(parseCommentStateAnswer(state({ state: "present", body: "b" }), "IC_1")).toEqual({
      state: "present",
      body: "b",
    });
    expect(parseCommentStateAnswer(state({ state: "absent" }), "IC_1")).toEqual({
      state: "absent",
    });
    // Another comment's answer, or one without the text it found, settles nothing.
    expect(parseCommentStateAnswer(state({ state: "absent" }), "IC_2")).toBeNull();
    expect(parseCommentStateAnswer(state({ state: "present" }), "IC_1")).toBeNull();
    expect(parseCommentStateAnswer(state({ state: "gone" }), "IC_1")).toBeNull();
  });
});

describe("pending reviews", () => {
  const EVERY_PENDING = [
    ...REVIEW_WRITE_OPERATIONS,
    ...PENDING_REVIEW_WRITE_OPERATIONS,
    PENDING_REVIEW_READ,
  ];

  async function pendingRoutable(
    operations: readonly string[] = EVERY_PENDING,
    codeHostWrite: unknown = vi.fn(),
  ) {
    await readyShell({
      writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...operations] },
      codeHostWrite,
    });
    readAs("mac-octo");
    setMacWritesReleaseFlagForTests(true);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    setReviewsFlagForTests(true);
    setPendingReviewsFlagForTests(true);
    return macWriteRoute(GITHUB_PR, null);
  }

  it("offers them where the shell lists every pending change, the read and the submit, with both flags on", async () => {
    const route = await pendingRoutable();
    expect(macPendingReviewOffers(route)).toBe(true);
    expect(macPendingReviewOffers(null)).toBe(false);
  });

  it.each([...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ, "pull_request_review_submit"])(
    "offers none on a partial shell without %s",
    async (missing) => {
      const route = await pendingRoutable(EVERY_PENDING.filter((op) => op !== missing));
      expect(macPendingReviewOffers(route)).toBe(false);
    },
  );

  it("offers none while either flag is off or unanswered", async () => {
    const route = await pendingRoutable();
    setPendingReviewsFlagForTests(false);
    expect(macPendingReviewOffers(route)).toBe(false);
    setPendingReviewsFlagForTests(null);
    expect(macPendingReviewOffers(route)).toBe(false);
    setPendingReviewsFlagForTests(true);
    setReviewsFlagForTests(false);
    expect(macPendingReviewOffers(route)).toBe(false);
  });

  it("offers none from a page that lacks one of the pending changes", async () => {
    const route = await pendingRoutable();
    setPagePendingReviewOperationsForTests(["pull_request_review_draft_add"]);
    expect(macPendingReviewOffers(route)).toBe(false);
  });

  it("holds a draft change, a discard, a submit and a single comment as one review change in flight", async () => {
    let answer!: (value: unknown) => void;
    const held = new Promise((resolve) => {
      answer = resolve;
    });
    const route = await pendingRoutable(EVERY_PENDING, () => held);
    const ticket = beginMacWrite(route!);
    const sent = sendMacWrite(ticket, "pull_request_review_draft_add", {}, () => null);
    for (const operation of [
      "pull_request_review_draft_edit",
      "pull_request_review_discard",
      "pull_request_review_submit",
      "pull_request_review_thread_create",
    ] as const) {
      expect(macWriteInFlight(operation, route!.target)).toBe(true);
    }
    expect(macWriteInFlight("pull_request_comment_edit", route!.target)).toBe(false);
    answer({ ok: false, error: { code: "cancelled", message: "x" } });
    await sent.catch(() => {});
    finishMacWrite(ticket);
  });

  it("carries a partial run's steps and cause, and an unconfirmed draft's baseline and start, to the error", async () => {
    const route = await pendingRoutable(EVERY_PENDING, async () => ({
      ok: false,
      error: { code: "outcome_unknown", message: "GitHub may have saved it." },
      steps: {
        completed: [{ step: "start_review", review_id: "PRR_1" }],
        uncertain: { step: "comment" },
        not_attempted: [],
      },
      baseline: {
        review_id: "PRR_1",
        thread_id: "PRRT_1",
        root_comment_id: "PRRC_root",
        complete: true,
        known_comment_ids: ["PRRC_old"],
      },
      start: { commit_oid: "c".repeat(40), prior_pending_review_id: null },
    }));
    const error = (await sendMacWrite(
      beginMacWrite(route!),
      "pull_request_review_draft_add",
      {},
      () => null,
    ).catch((caught: unknown) => caught)) as MacWriteError;
    expect(error.outcomeUnknown).toBe(true);
    expect(error.details).toEqual({
      steps: {
        completed: [{ step: "start_review", review_id: "PRR_1" }],
        uncertain: { step: "comment" },
        not_attempted: [],
      },
      draftBaseline: {
        reviewId: "PRR_1",
        scope: { kind: "reply", threadId: "PRRT_1", rootCommentId: "PRRC_root" },
        complete: true,
        knownCommentIds: ["PRRC_old"],
      },
      reviewStart: { commitOid: "c".repeat(40), priorPendingReviewId: null },
    });
  });

  it("carries a batch's results, refused before its sheet, for its caller to parse", async () => {
    const results = [
      { host: "github.com", repository: "acme/app", number: 1, outcome: "excluded" },
    ];
    installShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "nothing_to_enable", message: "None of these." },
        results,
      }),
      codeHostWriteCancel: vi.fn(),
    });
    const answer = await desktopCodeHostWrite({
      requestId: "req-1",
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_auto_merge_enable_many",
      params: {},
    });
    expect(answer).toEqual({
      ok: false,
      error: { code: "nothing_to_enable", message: "None of these." },
      results,
    });
  });

  it("reads a partial run's cause", async () => {
    installShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "partial", cause: "head_changed", message: "Started, but not added." },
        steps: {
          completed: [{ step: "start_review", review_id: "PRR_1" }],
          failed: { step: "comment" },
          not_attempted: [],
        },
      }),
      codeHostWriteCancel: vi.fn(),
    });
    const answer = await desktopCodeHostWrite({
      requestId: "req-1",
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_review_draft_add",
      params: {},
    });
    expect(answer).toEqual({
      ok: false,
      error: { code: "partial", cause: "head_changed", message: "Started, but not added." },
      steps: {
        completed: [{ step: "start_review", review_id: "PRR_1" }],
        failed: { step: "comment" },
        not_attempted: [],
      },
    });
  });

  it.each([
    ["steps of another shape", { steps: { completed: "start" } }],
    ["a step without its name", { steps: { completed: [{}], not_attempted: [] } }],
    [
      "a baseline without its ids",
      { baseline: { review_id: "PRR_1", path: "a", line: 1, start_line: null, complete: true } },
    ],
    [
      "a baseline with no scope",
      { baseline: { review_id: "PRR_1", complete: true, known_comment_ids: [] } },
    ],
    ["a start without its commit", { start: { prior_pending_review_id: null } }],
    ["a batch's results that aren't a list", { results: { number: 1 } }],
  ])("drops %s, as an older shell's answer that has none", async (_label, extra) => {
    installShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "outcome_unknown", message: "?" },
        ...extra,
      }),
      codeHostWriteCancel: vi.fn(),
    });
    const answer = await desktopCodeHostWrite({
      requestId: "req-1",
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_review_draft_add",
      params: {},
    });
    expect(answer).toEqual({ ok: false, error: { code: "outcome_unknown", message: "?" } });
  });

  it("reads a reply baseline without its root as incomplete", async () => {
    installShell({
      codeHostWrite: async () => ({
        ok: false,
        error: { code: "outcome_unknown", message: "?" },
        baseline: {
          review_id: "PRR_1",
          thread_id: "PRRT_1",
          root_comment_id: null,
          complete: true,
          known_comment_ids: [],
        },
      }),
      codeHostWriteCancel: vi.fn(),
    });
    const answer = await desktopCodeHostWrite({
      requestId: "req-1",
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 1 },
      operation: "pull_request_review_draft_add",
      params: {},
    });
    expect(answer).toMatchObject({ draftBaseline: { complete: false } });
  });

  it("reads a draft add's and a discard's answers strictly", () => {
    const comment = { ...REVIEW_COMMENT, pending: true };
    expect(
      parseDraftAddAnswer({
        object: "session.github.pr_review_draft",
        review_id: "PRR_1",
        thread: THREAD,
      }),
    ).toEqual({ review_id: "PRR_1", thread: THREAD });
    expect(
      parseDraftAddAnswer({
        object: "session.github.pr_review_draft",
        review_id: "PRR_1",
        thread_id: "PRRT_1",
        comment,
      }),
    ).toEqual({ review_id: "PRR_1", thread_id: "PRRT_1", comment });
    expect(
      parseDraftAddAnswer({ object: "session.github.pr_review_draft", thread: THREAD }),
    ).toBeNull();
    expect(
      parseDraftAddAnswer({
        object: "session.github.pr_review_draft",
        review_id: "PRR_1",
        thread: { id: "x" },
      }),
    ).toBeNull();
    expect(
      parseReviewDiscardAnswer({ object: "session.github.pr_review_discard", review_id: "PRR_1" }),
    ).toEqual({
      review_id: "PRR_1",
    });
    expect(
      parseReviewDiscardAnswer({ object: "session.github.pr_review", review_id: "PRR_1" }),
    ).toBeNull();
  });
});
