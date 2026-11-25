import type * as IdentityModule from "@/lib/identity";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
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

import {
  acceptMacAnswer,
  acceptRestoredMacAnswer,
  assertHostReadAllowed,
  automaticReadRoute,
  chooseReadSource,
  classifyPullRequestRead,
  DESKTOP_CODE_HOST_READS_FLAG,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsEnabled,
  githubMacReadsPreference,
  macAccountChanged,
  macAccountChoiceSaved,
  macAccountsChangedForTests,
  macReadFailed,
  macReadTicket,
  macViewerLogin,
  NO_DESKTOP_READS,
  PANEL_READ_OPERATIONS,
  macReadTarget,
  readChoiceFor,
  readOnSessionMachine,
  readOnThisMacAgain,
  readRouteFor,
  readPathSettled,
  readScope,
  useDesktopReadPathVersion,
  readsOnSessionMachine,
  readsOnThisMac,
  rememberGitHubHosts,
  resetDesktopReadPathForTests,
  type DesktopReadSupport,
} from "./desktopReadPath";
import type { ServerInfo } from "./capabilities";
import { CapabilitiesContext } from "./CapabilitiesContext";
import { BLOCKED_READ_SCOPE, ReadSourceUnavailableError } from "./pullRequestReadRoute";
import {
  resetAcceptedWorkspaceDesignForTests,
  setAcceptedWorkspaceDesign,
  useReportPullRequestsDesign,
} from "./workspaceSurfaces";
import {
  acceptMacRead,
  beginMacRead,
  macReadScopeKey,
  onMacReadScopeAdvanced,
} from "./macReadScope";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

// How many times a scope advanced in this test, i.e. how often reads had to start over.
let advances = 0;
// Listeners a test subscribed, unsubscribed after it.
const subscriptions: (() => void)[] = [];

const GITHUB_PR = "https://github.com/example-org/universe/pull/42";
const GHES_PR = "https://ghe.example.com/team/project/pull/7";
const SPOKE_PR = "https://2.spoke.example.com/example-org/universe/pull/20002346";
const GITLAB_MR = "https://gitlab.com/team/project/-/merge_requests/3";
const UNKNOWN_PR = "https://code.example.org/team/project/pull/9";

const CAPABLE: DesktopReadSupport = {
  github: { id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true },
  gitspoke: { id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] },
};

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

/** A shell that reads both providers and picks Mac accounts; `signedInHosts` answer account_list. */
function capableShell({
  signedInHosts = ["github.com"],
  codeHosts = [
    { id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true },
    { id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] },
  ] as unknown[],
  accountsBridge = true,
}: { signedInHosts?: string[]; codeHosts?: unknown[]; accountsBridge?: boolean } = {}) {
  const codeHostQuery = vi.fn(async (request: { operation: string; host: string }) =>
    request.operation === "account_list"
      ? {
          ok: true,
          data: signedInHosts.includes(request.host)
            ? [{ login: "octo", active: true, state: "success" }]
            : [],
        }
      : { ok: false, error: { code: "unsupported", message: "Not in this test." } },
  );
  installShell({
    codeHostQuery,
    ...(accountsBridge
      ? {
          codeHostAccounts: {
            get: async () => ({
              ok: true,
              data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
            }),
            set: async () => ({ ok: false }),
            onChanged: () => () => {},
          },
        }
      : {}),
    getDesktopFeatures: async () => ({ codeHosts }),
  });
  return codeHostQuery;
}

beforeEach(() => {
  advances = 0;
  subscriptions.push(
    onMacReadScopeAdvanced(() => {
      advances += 1;
    }),
  );
});

afterEach(() => {
  for (const unsubscribe of subscriptions.splice(0)) unsubscribe();
  resetDesktopReadPathForTests();
  resetAcceptedWorkspaceDesignForTests();
  identityListeners.clear();
  installShell(undefined);
  githubMacReadsPreference.write("automatic");
  currentUser.id = "user-1";
});

describe("githubMacReadsEnabled", () => {
  it.each([
    ["on", false, true],
    ["on", true, true],
    ["off", true, false],
    ["off", false, false],
    ["automatic", true, true],
    ["automatic", false, false],
  ] as const)("%s with the rollout %s reads on this Mac: %s", (intent, policy, expected) => {
    expect(githubMacReadsEnabled(intent, policy)).toBe(expected);
  });
});

describe("classifyPullRequestRead", () => {
  const classify = (url: string, options: Partial<Parameters<typeof classifyPullRequestRead>[1]>) =>
    classifyPullRequestRead(url, { support: CAPABLE, githubReadsEnabled: true, ...options })
      ?.provider ?? "host";

  it("reads github.com and GitSpoke on this Mac, and an Enterprise host only with evidence", () => {
    expect(classify(GITHUB_PR, {})).toBe("github");
    expect(classify(SPOKE_PR, {})).toBe("gitspoke");
    expect(classify(GHES_PR, {})).toBe("host");
    expect(classify(GHES_PR, { githubHosts: ["ghe.example.com"] })).toBe("github");
  });

  it("leaves GitLab and unknown hosts to the session's machine", () => {
    expect(classify(GITLAB_MR, { githubHosts: ["gitlab.com"] })).toBe("host");
    expect(classify(UNKNOWN_PR, {})).toBe("host");
  });

  it("follows the setting for GitHub only; GitSpoke has no other way to be read", () => {
    expect(classify(GITHUB_PR, { githubReadsEnabled: false })).toBe("host");
    expect(classify(SPOKE_PR, { githubReadsEnabled: false })).toBe("gitspoke");
  });

  it.each([
    ["no shell", NO_DESKTOP_READS],
    [
      "a shell without Mac accounts",
      { ...CAPABLE, github: { id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS] } },
    ],
    [
      "accounts without the account list",
      {
        ...CAPABLE,
        github: { id: "github", operations: [...PANEL_READ_OPERATIONS], accounts: true },
      },
    ],
    [
      "the account list without the panel's reads",
      { ...CAPABLE, github: { id: "github", operations: ["account_list"], accounts: true } },
    ],
  ] as const)("keeps GitHub on the host path with %s", (_name, support) => {
    expect(classify(GITHUB_PR, { support: support as DesktopReadSupport })).toBe("host");
  });
});

describe("the live read path", () => {
  it("is off for GitHub until the setting or the rollout turns it on", async () => {
    capableShell();
    expect(await desktopReadTarget(GITHUB_PR)).toBeNull();
    githubMacReadsPreference.write("on");
    expect(await desktopReadTarget(GITHUB_PR)).toMatchObject({ provider: "github" });
    expect(readsOnThisMac(GITHUB_PR)).toBe(true);
    expect(await desktopReadTarget(SPOKE_PR)).toMatchObject({ provider: "gitspoke" });
  });

  it("hears the rollout anew after a reset, not the flag's last value", async () => {
    const featureFlags = {
      subscribe: (name: string, _options: unknown, callback: (value: unknown) => void) => {
        if (name === DESKTOP_CODE_HOST_READS_FLAG) queueMicrotask(() => callback(true));
        return () => {};
      },
    };
    /** The capable shell, with the rollout flag listed and answered through `featureFlags`. */
    const shell = () => {
      capableShell();
      const desktop = (window as unknown as { omnigentDesktop: Record<string, unknown> })
        .omnigentDesktop;
      const features = desktop.getDesktopFeatures as () => Promise<Record<string, unknown>>;
      desktop.getDesktopFeatures = async () => ({
        ...(await features()),
        featureFlags: { names: [DESKTOP_CODE_HOST_READS_FLAG] },
      });
      desktop.featureFlags = featureFlags;
    };
    shell();
    await vi.waitFor(async () =>
      expect(await desktopReadTarget(GITHUB_PR)).toMatchObject({ provider: "github" }),
    );

    // The rollout now never answers: until it does, GitHub stays on the session's machine.
    resetDesktopReadPathForTests();
    featureFlags.subscribe = () => () => {};
    shell();
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(await desktopReadTarget(GITHUB_PR)).toBeNull();
  });

  it("counts an Enterprise host once this Mac's gh is signed in to it, or the session vouches", async () => {
    const query = capableShell({ signedInHosts: ["ghe.example.com"] });
    githubMacReadsPreference.write("on");
    expect(await desktopReadTarget(GHES_PR)).toMatchObject({
      provider: "github",
      host: "ghe.example.com",
    });
    // A host the Mac isn't signed in to stays on the host path until its accounts change.
    const asked = () =>
      query.mock.calls.filter(([request]) => request.host === "code.example.org").length;
    expect(await desktopReadTarget(UNKNOWN_PR)).toBeNull();
    expect(await desktopReadTarget(UNKNOWN_PR)).toBeNull();
    expect(asked()).toBe(1);
    macAccountChanged("github", "code.example.org");
    expect(await desktopReadTarget(UNKNOWN_PR)).toBeNull();
    expect(asked()).toBe(2);
    rememberGitHubHosts(["code.example.org"]);
    expect(await desktopReadTarget(UNKNOWN_PR)).toMatchObject({ provider: "github" });
  });

  it("settles an Enterprise PR's path only once this Mac's gh has answered for its host", async () => {
    const query = capableShell({ signedInHosts: ["ghe.example.com"] });
    githubMacReadsPreference.write("on");
    expect(readPathSettled(GHES_PR)).toBe(false);
    await desktopReadTarget(GITHUB_PR);
    // Asking settles it without the session's machine, and the target comes with it.
    expect(readPathSettled(GHES_PR)).toBe(false);
    await vi.waitFor(() => expect(readPathSettled(GHES_PR)).toBe(true));
    expect(macReadTarget(GHES_PR)).toMatchObject({ host: "ghe.example.com" });
    expect(query).toHaveBeenCalledTimes(1);
    // github.com, GitSpoke and a host the Mac isn't signed in to settle without waiting.
    expect(readPathSettled(GITHUB_PR)).toBe(true);
    expect(readPathSettled(SPOKE_PR)).toBe(true);
    expect(readPathSettled(UNKNOWN_PR)).toBe(false);
    await vi.waitFor(() => expect(readPathSettled(UNKNOWN_PR)).toBe(true));
    expect(macReadTarget(UNKNOWN_PR)).toBeNull();
  });

  it("never asks gh about a host while GitHub reads are off", async () => {
    const query = capableShell({ signedInHosts: ["ghe.example.com"] });
    githubMacReadsPreference.write("off");
    expect(await desktopReadTarget(GHES_PR)).toBeNull();
    expect(query).not.toHaveBeenCalled();
  });

  it("keeps GitHub on the host path on a shell that lists accounts without the bridge", async () => {
    capableShell({ accountsBridge: false });
    githubMacReadsPreference.write("on");
    expect(await desktopReadTarget(GITHUB_PR)).toBeNull();
    expect(await desktopReadTarget(SPOKE_PR)).toMatchObject({ provider: "gitspoke" });
  });

  it("uses the host path in browsers", async () => {
    installShell(undefined);
    githubMacReadsPreference.write("on");
    expect(await desktopReadTarget(GITHUB_PR)).toBeNull();
    expect(await desktopReadTarget(SPOKE_PR)).toBeNull();
    expect(readScope(GITHUB_PR)).toEqual([]);
  });

  it("sends a PR back to the session's machine for this run, and back again", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    readOnSessionMachine(GITHUB_PR);
    expect(readsOnThisMac(GITHUB_PR)).toBe(false);
    expect(readsOnSessionMachine(GITHUB_PR)).toBe(true);
    readOnThisMacAgain(GITHUB_PR);
    expect(readsOnThisMac(GITHUB_PR)).toBe(true);
  });
});

describe("binding Mac answers to their account and workspace user", () => {
  const identity = (login: string, generation: number) => ({
    host: "github.com",
    login,
    generation,
  });
  const answer = (login: string, generation: number) => ({ identity: identity(login, generation) });
  const ticket = () => macReadTicket("github", "github.com");
  let advanced: (readonly unknown[])[] = [];

  async function readyStore() {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    advanced = [];
    advances = 0;
    subscriptions.push(onMacReadScopeAdvanced((scope) => advanced.push(scope)));
  }

  it("keys every Mac read by user, provider and host, never by account", async () => {
    await readyStore();
    expect(readScope(GITHUB_PR)).toEqual(["mac-read", "user-1", "github", "github.com"]);
    expect(macReadScopeKey("gitspoke", "2.spoke.example.com")).toEqual([
      "mac-read",
      "user-1",
      "gitspoke",
      "2.spoke.example.com",
    ]);
    acceptMacAnswer(ticket(), answer("alice", 1));
    acceptMacAnswer(ticket(), answer("bob", 2));
    expect(readScope(GITHUB_PR)).toEqual(["mac-read", "user-1", "github", "github.com"]);
  });

  it("keeps the first answer and records who read it, without advancing", async () => {
    await readyStore();
    expect(acceptMacAnswer(ticket(), answer("alice", 1))).toBe("accepted");
    expect(advances).toBe(0);
    expect(advanced).toEqual([]);
  });

  it("drops an answer read as another account and advances; a late one doesn't advance again", async () => {
    await readyStore();
    acceptMacAnswer(ticket(), answer("alice", 1));
    expect(acceptMacAnswer(ticket(), answer("bob", 2))).toBe("moved");
    expect(advances).toBe(1);
    expect(advanced).toEqual([["mac-read", "user-1", "github", "github.com"]]);
    // An older answer still arriving after the switch is dropped without moving it again.
    expect(acceptMacAnswer(ticket(), answer("alice", 1))).toBe("moved");
    expect(advances).toBe(1);
  });

  it("drops an answer that lands after the workspace user changed", async () => {
    await readyStore();
    const sent = ticket();
    currentUser.id = "user-2";
    expect(acceptMacAnswer(sent, answer("alice", 1))).toBe("moved");
    expect(readScope(GITHUB_PR)).toEqual(["mac-read", "user-2", "github", "github.com"]);
  });

  it("keeps answers when a saved choice leaves the same account in use, and advances otherwise", async () => {
    await readyStore();
    acceptMacAnswer(ticket(), answer("alice", 1));
    const accounts = [{ login: "alice", active: true, state: "success" as const }];
    macAccountChoiceSaved("github.com", {
      intent: { mode: "manual", login: "alice" },
      effective: { login: "alice", generation: 1 },
      accounts,
    });
    expect(advances).toBe(0);
    macAccountChoiceSaved("github.com", {
      intent: { mode: "manual", login: "bob" },
      effective: { login: "bob", generation: 2 },
      accounts,
    });
    expect(advances).toBe(1);
  });

  it("keeps the first answer when main's first resolution reaches the page before it", async () => {
    await readyStore();
    const sent = ticket();
    // Main resolved the account for this read and said so before answering it.
    macAccountsChangedForTests("github.com", 1);
    expect(acceptMacAnswer(sent, answer("alice", 1))).toBe("accepted");
    expect(advances).toBe(0);
  });

  it("reads again after a failed read once the account changes, even with none kept yet", async () => {
    await readyStore();
    macReadFailed(ticket(), "network");
    // Signed out at first; then a sign-in, or choosing a working account, reaches the page.
    macAccountsChangedForTests("github.com", 1);
    expect(advances).toBe(1);
    // Once read again, a further first-resolution event keeps the answer.
    const sent = ticket();
    macAccountsChangedForTests("github.com", 2);
    expect(acceptMacAnswer(sent, answer("alice", 2))).toBe("accepted");
  });

  it("re-reads an answer older than the change main reported before any was kept", async () => {
    await readyStore();
    const sent = ticket();
    macAccountsChangedForTests("github.com", 2);
    expect(acceptMacAnswer(sent, answer("alice", 1))).toBe("moved");
    expect(advances).toBe(1);
  });

  describe("an answer saved on this device", () => {
    it("shows again as the account main resolves now, which then binds the scope", async () => {
      await readyStore();
      expect(acceptRestoredMacAnswer(ticket(), identity("alice", 1), "Alice")).toBe(true);
      expect(advances).toBe(0);
      // Bound like a live answer: a switch to another account drops what was restored.
      macAccountsChangedForTests("github.com", 2);
      expect(advances).toBe(1);
    });

    it("is refused when this Mac reads GitHub as someone else now", async () => {
      await readyStore();
      expect(acceptRestoredMacAnswer(ticket(), identity("bob", 1), "alice")).toBe(false);
      // Nothing was bound, so the next live answer is the scope's first.
      expect(acceptMacAnswer(ticket(), answer("bob", 1))).toBe("accepted");
    });

    it("is refused once the workspace user changed while the shell answered", async () => {
      await readyStore();
      const sent = ticket();
      currentUser.id = "user-2";
      expect(acceptRestoredMacAnswer(sent, identity("alice", 1), "alice")).toBe(false);
    });

    it("is refused when the account changed while the shell answered", async () => {
      await readyStore();
      const sent = ticket();
      macAccountsChangedForTests("github.com", 2);
      // Main's answer named the account from before the change.
      expect(acceptRestoredMacAnswer(sent, identity("alice", 1), "alice")).toBe(false);
    });

    it("after a switch away and back, shows only for main's answer as the account now", async () => {
      await readyStore();
      const stale = ticket();
      macAccountsChangedForTests("github.com", 2);
      macAccountsChangedForTests("github.com", 3);
      expect(acceptRestoredMacAnswer(stale, identity("alice", 1), "alice")).toBe(false);
      expect(acceptRestoredMacAnswer(ticket(), identity("alice", 3), "alice")).toBe(true);
    });

    it("is refused for a scope that already moved since the ticket", async () => {
      await readyStore();
      const sent = ticket();
      macAccountChanged("github", "github.com");
      expect(acceptRestoredMacAnswer(sent, identity("alice", 1), "alice")).toBe(false);
    });
  });

  it("drops an answer sent before the shell said the account changed", async () => {
    await readyStore();
    const sent = ticket();
    macAccountChanged("github", "github.com");
    expect(acceptMacAnswer(sent, answer("alice", 1))).toBe("moved");
    expect(advances).toBe(1);
  });

  it("keeps concurrent answers as the same account in one scope", async () => {
    await readyStore();
    const [first, second, third] = [ticket(), ticket(), ticket()];
    expect(acceptMacAnswer(third, answer("alice", 1))).toBe("accepted");
    expect(acceptMacAnswer(first, answer("alice", 1))).toBe("accepted");
    expect(acceptMacAnswer(second, answer("alice", 1))).toBe("accepted");
    expect(advances).toBe(0);
  });

  it("settles a PR sent to the session's machine without asking this Mac again", async () => {
    const query = capableShell({ signedInHosts: ["github.com", "ghe.example.com"] });
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GHES_PR);
    for (const url of [GITHUB_PR, GHES_PR]) {
      readOnSessionMachine(url);
      expect(readPathSettled(url)).toBe(true);
      expect(macReadTarget(url)).toBeNull();
    }
    expect(
      query.mock.calls.filter(([request]) => request.operation === "account_list"),
    ).toHaveLength(1);
  });
});

describe("checking who read a Mac answer", () => {
  const SPOKE = "2.spoke.example.com";
  const provenance = (provider: "github" | "gitspoke", host: string, viewer: string) => ({
    version: 1 as const,
    provider,
    host,
    viewer,
  });
  const spoke = (viewer: string) => ({ provenance: provenance("gitspoke", SPOKE, viewer) });
  const spokeTicket = () => macReadTicket("gitspoke", SPOKE);

  async function provingShell() {
    capableShell({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          answerProvenance: 1,
        },
        { id: "gitspoke", operations: [...PANEL_READ_OPERATIONS], answerProvenance: 1 },
      ],
    });
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    advances = 0;
  }

  it("establishes a spoke's viewer from its first answer", async () => {
    await provingShell();
    expect(acceptMacAnswer(spokeTicket(), spoke("alice"))).toBe("accepted");
    expect(acceptMacAnswer(spokeTicket(), spoke("alice"))).toBe("accepted");
    expect(advances).toBe(0);
  });

  it("moves to a newer viewer, and refuses a late answer from the previous one", async () => {
    await provingShell();
    acceptMacAnswer(spokeTicket(), spoke("alice"));
    const late = spokeTicket();
    const newer = spokeTicket();
    // B answers before the late A read: kept, and what was cached as A goes.
    expect(acceptMacAnswer(newer, spoke("bob"))).toBe("accepted");
    expect(advances).toBe(1);
    expect(acceptMacAnswer(late, spoke("alice"))).toBe("moved");
    expect(acceptMacAnswer(spokeTicket(), spoke("bob"))).toBe("accepted");
    expect(advances).toBe(1);
  });

  it("shares one scope with list stats, so neither keeps a read from before the other's switch", async () => {
    await provingShell();
    const key = macReadScopeKey("gitspoke", SPOKE);
    const listRead = beginMacRead(key);
    expect(acceptMacRead(beginMacRead(key), "alice")).toBe("accepted");
    // A newer panel read as bob changes the reader the list stats share.
    expect(acceptMacAnswer(spokeTicket(), spoke("bob"))).toBe("accepted");
    expect(acceptMacRead(listRead, "alice")).toBe("rejected");
    // And a list read as carol after that refuses the panel's earlier read.
    const panelRead = spokeTicket();
    expect(acceptMacRead(beginMacRead(key), "carol")).toBe("transition");
    expect(acceptMacAnswer(panelRead, spoke("bob"))).toBe("moved");
    expect(acceptMacAnswer(spokeTicket(), spoke("carol"))).toBe("accepted");
  });

  it("follows A, then B, then A again, one move each", async () => {
    await provingShell();
    expect(acceptMacAnswer(spokeTicket(), spoke("alice"))).toBe("accepted");
    // Each newer answer from another viewer is kept, and moves the scope to it.
    expect(acceptMacAnswer(spokeTicket(), spoke("bob"))).toBe("accepted");
    expect(acceptMacAnswer(spokeTicket(), spoke("alice"))).toBe("accepted");
    expect(advances).toBe(2);
    expect(acceptMacAnswer(spokeTicket(), spoke("alice"))).toBe("accepted");
    expect(advances).toBe(2);
  });

  it("refuses a late answer from another viewer without moving the scope", async () => {
    await provingShell();
    const older = spokeTicket();
    acceptMacAnswer(spokeTicket(), spoke("alice"));
    expect(acceptMacAnswer(older, spoke("bob"))).toBe("moved");
    expect(advances).toBe(0);
  });

  it("refuses an answer a proving shell sent without provenance, or for another host", async () => {
    await provingShell();
    expect(acceptMacAnswer(spokeTicket(), {})).toBe("unproven");
    expect(
      acceptMacAnswer(spokeTicket(), {
        provenance: provenance("gitspoke", "3.spoke.example.com", "a"),
      }),
    ).toBe("unproven");
    expect(
      acceptMacAnswer(spokeTicket(), {
        provenance: { ...provenance("gitspoke", SPOKE, "a"), version: 2 as 1 },
      }),
    ).toBe("unproven");
    // Not a change of identity: the scope stays.
    expect(advances).toBe(0);
  });

  it("refuses a GitHub answer whose viewer isn't the account main resolved", async () => {
    await provingShell();
    const sent = macReadTicket("github", "github.com");
    expect(
      acceptMacAnswer(sent, {
        identity: { host: "github.com", login: "alice", generation: 1 },
        provenance: provenance("github", "github.com", "mallory"),
      }),
    ).toBe("moved");
    expect(advances).toBe(1);
    expect(
      acceptMacAnswer(macReadTicket("github", "github.com"), {
        identity: { host: "github.com", login: "alice", generation: 1 },
        provenance: provenance("github", "github.com", "Alice"),
      }),
    ).toBe("accepted");
  });

  it("keeps an older shell's answers, which carry no provenance", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    expect(acceptMacAnswer(spokeTicket(), {})).toBe("accepted");
    expect(
      acceptMacAnswer(macReadTicket("github", "github.com"), {
        identity: { host: "github.com", login: "alice", generation: 1 },
      }),
    ).toBe("accepted");
  });

  it("advances every scope when the workspace user changes", async () => {
    await provingShell();
    acceptMacAnswer(spokeTicket(), spoke("alice"));
    const advanced: (readonly unknown[])[] = [];
    subscriptions.push(onMacReadScopeAdvanced((scope) => advanced.push(scope)));
    currentUser.id = "user-2";
    for (const listener of identityListeners) listener();
    // The previous user's reads are the ones dropped.
    expect(advanced).toContainEqual(["mac-read", "user-1", "gitspoke", SPOKE]);
    // The new user's first spoke read establishes its own viewer.
    expect(acceptMacAnswer(spokeTicket(), spoke("carol"))).toBe("accepted");
  });

  it.each([
    ["a spoke", () => spokeTicket(), spoke("alice")],
    [
      "GitHub",
      () => macReadTicket("github", "github.com"),
      {
        identity: { host: "github.com", login: "alice", generation: 1 },
        provenance: provenance("github", "github.com", "alice"),
      },
    ],
  ])(
    "refuses %s's first read still in flight across a user switch and back",
    async (_name, sendRead, answer) => {
      await provingShell();
      const sent = sendRead();
      const switchTo = (id: string) => {
        currentUser.id = id;
        for (const listener of [...identityListeners]) listener();
      };
      switchTo("user-2");
      switchTo("user-1");
      expect(acceptMacAnswer(sent, answer)).toBe("moved");
      expect(acceptMacAnswer(sendRead(), answer)).toBe("accepted");
    },
  );
});

describe("a pull request's read choice", () => {
  const GITSPOKE_ONLY = [{ id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] }];

  it.each([
    // Settings, shell, choice → the route's kind.
    ["off", "capable", "automatic", "host"],
    ["off", "capable", "mac", "mac"],
    ["off", "capable", "session", "host"],
    ["on", "capable", "automatic", "mac"],
    ["on", "capable", "mac", "mac"],
    ["on", "capable", "session", "host"],
    ["automatic", "capable", "automatic", "host"],
    ["automatic", "capable", "mac", "mac"],
    ["on", "gitspoke-only", "automatic", "host"],
    ["on", "gitspoke-only", "mac", "blocked"],
    ["on", "gitspoke-only", "session", "host"],
  ] as const)(
    "with Settings %s and a %s shell, choosing %s reads on: %s",
    async (setting, shell, choice, kind) => {
      capableShell(shell === "capable" ? {} : { codeHosts: GITSPOKE_ONLY });
      githubMacReadsPreference.write(setting);
      await desktopReadTarget(GITHUB_PR);
      chooseReadSource(GITHUB_PR, choice);
      expect(readRouteFor(GITHUB_PR).kind).toBe(kind);
      expect(readsOnThisMac(GITHUB_PR)).toBe(kind === "mac");
    },
  );

  it("works a PR's route out once per change to the store, and follows every change", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    const route = readRouteFor(GITHUB_PR);
    expect(route.kind).toBe("mac");
    // Asked again with nothing changed: the same answer, not worked out anew.
    expect(readRouteFor(GITHUB_PR)).toBe(route);
    githubMacReadsPreference.write("off");
    expect(readRouteFor(GITHUB_PR).kind).toBe("host");
    chooseReadSource(GITHUB_PR, "mac");
    expect(readRouteFor(GITHUB_PR).kind).toBe("mac");
    chooseReadSource(GITHUB_PR, "session");
    expect(readRouteFor(GITHUB_PR).kind).toBe("host");
    githubMacReadsPreference.write("on");
    chooseReadSource(GITHUB_PR, "automatic");
    expect(readRouteFor(GITHUB_PR)).not.toBe(route);
    expect(readRouteFor(GITHUB_PR)).toEqual(route);
  });

  it("never reads a PR chosen for this Mac on the host before the shell answers, or in a browser", () => {
    installShell(undefined);
    chooseReadSource(GITHUB_PR, "mac");
    expect(readRouteFor(GITHUB_PR)).toEqual({ kind: "blocked", reason: "mac_unsupported" });
    expect(readScope(GITHUB_PR)).toEqual(BLOCKED_READ_SCOPE);
    expect(() => assertHostReadAllowed(GITHUB_PR)).toThrow(ReadSourceUnavailableError);
    // Automatic and the session's machine still read on the host.
    chooseReadSource(GITHUB_PR, "session");
    expect(() => assertHostReadAllowed(GITHUB_PR)).not.toThrow();
    expect(readScope(GITHUB_PR)).toEqual([]);
  });

  it("says what Automatic would pick, whatever the user chose", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    chooseReadSource(GITHUB_PR, "session");
    expect(automaticReadRoute(GITHUB_PR)).toBe("mac");
    githubMacReadsPreference.write("off");
    chooseReadSource(GITHUB_PR, "mac");
    expect(automaticReadRoute(GITHUB_PR)).toBe("host");
    expect(readRouteFor(GITHUB_PR).kind).toBe("mac");
  });

  it.each([
    [
      "the owner and repository in another case",
      (url: string) => url.replace("example-org/universe", "Example-Org/Universe"),
    ],
    ["a subpage", (url: string) => `${url}/files`],
    ["the commits subpage", (url: string) => `${url}/commits`],
    ["a query and a fragment", (url: string) => `${url}?w=1#discussion_r1`],
    ["a trailing slash", (url: string) => `${url}/`],
  ])("keeps one choice and one route per PR, with %s", async (_name, spell) => {
    capableShell({ codeHosts: [{ id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] }] });
    await desktopReadTarget(GITHUB_PR);
    chooseReadSource(spell(GITHUB_PR), "mac");
    for (const url of [GITHUB_PR, spell(GITHUB_PR)]) {
      expect(readChoiceFor(url)).toBe("mac");
      expect(readRouteFor(url).kind).toBe("blocked");
      expect(() => assertHostReadAllowed(url)).toThrow(ReadSourceUnavailableError);
    }
    chooseReadSource(GITHUB_PR, "automatic");
    expect(readChoiceFor(spell(GITHUB_PR))).toBe("automatic");
    expect(() => assertHostReadAllowed(spell(GITHUB_PR))).not.toThrow();
  });

  it("keeps one choice for an Enterprise PR however it's spelled, before and after its host is known", async () => {
    capableShell({ signedInHosts: ["ghe.example.com"] });
    githubMacReadsPreference.write("on");
    chooseReadSource(`${GHES_PR.replace("team/project", "Team/Project")}/files`, "session");
    expect(readChoiceFor(GHES_PR)).toBe("session");
    await desktopReadTarget(GHES_PR);
    expect(readRouteFor(`${GHES_PR}#top`).kind).toBe("host");
  });

  it("keeps the classic page's session-machine helpers meaning what they did", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    readOnSessionMachine(GITHUB_PR);
    expect(readChoiceFor(GITHUB_PR)).toBe("session");
    readOnThisMacAgain(GITHUB_PR);
    expect(readChoiceFor(GITHUB_PR)).toBe("automatic");
    // Undoing a session choice never undoes an explicit Mac choice.
    chooseReadSource(GITHUB_PR, "mac");
    readOnThisMacAgain(GITHUB_PR);
    expect(readChoiceFor(GITHUB_PR)).toBe("mac");
  });

  it.each([
    // Automatic would still read both on this Mac: GitHub with Mac reads on, and GitSpoke.
    ["GitHub with Mac reads on", (): string => GITHUB_PR, "github", "github.com"],
    ["GitSpoke", (): string => SPOKE_PR, "gitspoke", "2.spoke.example.com"],
  ] as const)(
    "keeps a Mac choice on %s through Classic, which reads it on the session's machine, and applies it again under Tabs",
    async (_label, prUrl, provider, host) => {
      capableShell();
      githubMacReadsPreference.write("on");
      const url = prUrl();
      await desktopReadTarget(url);
      setAcceptedWorkspaceDesign("tabs");
      expect(automaticReadRoute(url)).toBe("mac");
      chooseReadSource(url, "mac");
      const macScope = macReadScopeKey(provider, host);
      expect(readRouteFor(url).kind).toBe("mac");
      expect(readScope(url)).toEqual(macScope);

      setAcceptedWorkspaceDesign("classic");
      expect(readChoiceFor(url)).toBe("mac");
      expect(readRouteFor(url)).toEqual({ kind: "host" });
      expect(readScope(url)).toEqual([]);
      expect(readsOnThisMac(url)).toBe(false);
      expect(() => assertHostReadAllowed(url)).not.toThrow();

      setAcceptedWorkspaceDesign("tabs");
      expect(readRouteFor(url).kind).toBe("mac");
      expect(readScope(url)).toEqual(macScope);
    },
  );

  it("reads a saved Mac choice on the session's machine while the tabs sidebar shows the classic PR page", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    setAcceptedWorkspaceDesign("tabs");
    chooseReadSource(GITHUB_PR, "mac");
    expect(readRouteFor(GITHUB_PR).kind).toBe("mac");
    // The app shell reports the design: the new sidebar on, Pull Requests v2 off.
    const features: Record<string, boolean> = { workspace_sidebar_v2: true };
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        CapabilitiesContext.Provider,
        { value: { features: { ...features } } as unknown as ServerInfo },
        children,
      );
    const { rerender } = renderHook(() => useReportPullRequestsDesign(), { wrapper });
    expect(readChoiceFor(GITHUB_PR)).toBe("mac");
    expect(readRouteFor(GITHUB_PR)).toEqual({ kind: "host" });
    expect(readsOnThisMac(GITHUB_PR)).toBe(false);
    // Turning Pull Requests v2 on shows the new page, which applies the choice again.
    features.pull_requests_v2 = true;
    rerender();
    expect(readRouteFor(GITHUB_PR).kind).toBe("mac");
  });

  it("leaves a session-machine choice and Automatic routes alone under Classic", async () => {
    capableShell();
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    setAcceptedWorkspaceDesign("classic");
    // Classic shows the session-machine choice, so it still applies there.
    chooseReadSource(SPOKE_PR, "session");
    expect(readsOnSessionMachine(SPOKE_PR)).toBe(true);
    expect(readRouteFor(SPOKE_PR).kind).toBe("host");
    // Nothing chosen: Automatic keeps reading on this Mac where it would.
    expect(readRouteFor(GITHUB_PR).kind).toBe("mac");
  });

  it("re-renders readers when the accepted design moves a choice's route", async () => {
    capableShell();
    githubMacReadsPreference.write("off");
    await desktopReadTarget(GITHUB_PR);
    setAcceptedWorkspaceDesign("tabs");
    chooseReadSource(GITHUB_PR, "mac");
    const { result } = renderHook(() => useDesktopReadPathVersion());
    const before = result.current;
    act(() => setAcceptedWorkspaceDesign("classic"));
    expect(result.current).toBeGreaterThan(before);
  });

  it("keeps an unsupported Mac choice blocked under Tabs, and reads it on the session's machine under Classic", async () => {
    capableShell({ codeHosts: [{ id: "gitspoke", operations: [...PANEL_READ_OPERATIONS] }] });
    await desktopReadTarget(GITHUB_PR);
    setAcceptedWorkspaceDesign("tabs");
    chooseReadSource(GITHUB_PR, "mac");
    expect(readRouteFor(GITHUB_PR)).toEqual({ kind: "blocked", reason: "mac_unsupported" });
    expect(readScope(GITHUB_PR)).toEqual(BLOCKED_READ_SCOPE);
    setAcceptedWorkspaceDesign("classic");
    // This Mac can't read GitHub, so nothing reads here, as anyone or by name.
    expect(readRouteFor(GITHUB_PR)).toEqual({ kind: "host" });
    expect(readsOnThisMac(GITHUB_PR)).toBe(false);
    expect(readChoiceFor(GITHUB_PR)).toBe("mac");
  });

  it("lands a Mac answer pending across a design switch only in the Mac scope", async () => {
    capableShell();
    githubMacReadsPreference.write("off");
    await desktopReadTarget(GITHUB_PR);
    setAcceptedWorkspaceDesign("tabs");
    chooseReadSource(GITHUB_PR, "mac");
    const ticket = macReadTicket("github", "github.com");
    setAcceptedWorkspaceDesign("classic");
    acceptMacAnswer(ticket, { identity: { host: "github.com", login: "octo", generation: 1 } });
    // The host route's key never carries a Mac read's answer.
    expect(readScope(GITHUB_PR)).toEqual([]);
    expect(macViewerLogin(GITHUB_PR)).toBeNull();
    setAcceptedWorkspaceDesign("tabs");
    expect(macViewerLogin(GITHUB_PR)).toBe("octo");
  });

  it("forgets every choice when the workspace user changes", async () => {
    capableShell();
    await desktopReadTarget(GITHUB_PR);
    chooseReadSource(GITHUB_PR, "mac");
    chooseReadSource(SPOKE_PR, "session");
    currentUser.id = "user-2";
    for (const listener of identityListeners) listener();
    expect(readChoiceFor(GITHUB_PR)).toBe("automatic");
    expect(readChoiceFor(SPOKE_PR)).toBe("automatic");
  });

  it("lets an explicit Mac choice find an Enterprise host Settings leave off", async () => {
    const query = capableShell({ signedInHosts: ["ghe.example.com"] });
    githubMacReadsPreference.write("off");
    expect(await desktopReadTarget(GHES_PR)).toBeNull();
    expect(query).not.toHaveBeenCalled();
    chooseReadSource(GHES_PR, "mac");
    expect(await desktopReadTarget(GHES_PR)).toMatchObject({ host: "ghe.example.com" });
  });

  it.each(["files", "commits"])(
    "probes an unrecognized Enterprise host once, first through its /%s page, for an explicit Mac",
    async (subpage) => {
      const query = capableShell({ signedInHosts: ["ghe.example.com"] });
      githubMacReadsPreference.write("off");
      const url = `${GHES_PR.replace("team/project", "Team/Project")}/${subpage}`;
      chooseReadSource(url, "mac");
      expect(await desktopReadTarget(url)).toMatchObject({ host: "ghe.example.com" });
      expect(readRouteFor(url).kind).toBe("mac");
      expect(readPathSettled(url)).toBe(true);
      const probes = query.mock.calls.filter(([request]) => request.operation === "account_list");
      expect(probes).toHaveLength(1);
    },
  );
});

describe("macViewerLogin", () => {
  const SPOKE = "2.spoke.example.com";
  const github = (login: string, host = "github.com", generation = 1) => ({
    identity: { host, login, generation },
  });

  async function readingShell({ namedReads = false } = {}) {
    capableShell({
      signedInHosts: ["github.com", "ghe.example.com"],
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          ...(namedReads ? { readAs: 1 } : {}),
        },
        { id: "gitspoke", operations: [...PANEL_READ_OPERATIONS], answerProvenance: 1 },
      ],
    });
    githubMacReadsPreference.write("on");
    await desktopReadTarget(GITHUB_PR);
    await desktopReadTarget(GHES_PR);
  }

  it("names each host's own reader, and nobody before a read or off this Mac", async () => {
    await readingShell();
    expect(macViewerLogin(GITHUB_PR)).toBeNull();
    acceptMacAnswer(macReadTicket("github", "github.com"), github("Guido-B_data"));
    acceptMacAnswer(macReadTicket("github", "ghe.example.com"), github("guido", "ghe.example.com"));
    acceptMacAnswer(macReadTicket("gitspoke", SPOKE), {
      provenance: { version: 1, provider: "gitspoke", host: SPOKE, viewer: "gbo" },
    });
    expect(macViewerLogin(GITHUB_PR)).toBe("Guido-B_data");
    expect(macViewerLogin(GHES_PR)).toBe("guido");
    expect(macViewerLogin(SPOKE_PR)).toBe("gbo");
    readOnSessionMachine(GITHUB_PR);
    expect(macViewerLogin(GITHUB_PR)).toBeNull();
    expect(macViewerLogin(GITLAB_MR)).toBeNull();
  });

  it("follows the Mac account when Automatic switches to another", async () => {
    await readingShell();
    acceptMacAnswer(macReadTicket("github", "github.com"), github("alice", "github.com", 1));
    for (let read = 0; read < 2; read += 1) {
      acceptMacAnswer(macReadTicket("github", "github.com"), github("bob", "github.com", 2));
    }
    expect(macViewerLogin(GITHUB_PR)).toBe("bob");
  });

  it("prefers the page's named reader on its own host only", async () => {
    await readingShell({ namedReads: true });
    acceptMacAnswer(macReadTicket("github", "github.com"), github("alice"));
    acceptMacAnswer(macReadTicket("github", "ghe.example.com"), github("eve", "ghe.example.com"));
    const reader = { host: "github.com", login: "bob" };
    expect(macViewerLogin(GITHUB_PR, reader)).toBe("bob");
    expect(macViewerLogin(GHES_PR, reader)).toBe("eve");
  });
});
