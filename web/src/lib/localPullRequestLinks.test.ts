import { afterEach, describe, expect, it, vi } from "vitest";

const { currentUser } = vi.hoisted(() => ({ currentUser: { id: "user-1" as string | null } }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => currentUser.id }));

import {
  GITSPOKE_READ_OPERATIONS,
  canKeepLinksOnThisMac,
  forgetLocalPullRequestServer,
  forgetLocalPullRequestSession,
  isLocalPullRequest,
  linkPullRequestOnThisMac,
  listLocalPullRequests,
  listLocalPullRequestsByUser,
  localOnlyPullRequests,
  removeLocalPullRequest,
  withLocalPullRequests,
} from "./localPullRequestLinks";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

// The session and PR from the GitSpoke acceptance check.
const SESSION = "3869946998045662";
const SPOKE_PR = "https://2.spoke.example.com/example-org/universe/pull/20002346";
const LINK = {
  url: SPOKE_PR,
  provider: "gitspoke",
  host: "2.spoke.example.com",
  repository: "example-org/universe",
  number: 20002346,
  linked_at: 1,
};

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

function capableShell(overrides: Record<string, unknown> = {}) {
  const localPullRequestLinks = vi.fn(async (request: { action: string }) => ({
    ok: true,
    data: request.action === "remove" ? [] : [LINK],
  }));
  const codeHostQuery = vi.fn(async () => ({ ok: true, data: { number: 20002346 } }));
  installShell({
    localPullRequestLinks,
    codeHostQuery,
    getDesktopFeatures: async () => ({
      localPullRequestLinks: true,
      codeHosts: [{ id: "gitspoke", operations: [...GITSPOKE_READ_OPERATIONS] }],
    }),
    ...overrides,
  });
  return { localPullRequestLinks, codeHostQuery };
}

afterEach(() => {
  installShell(undefined);
  currentUser.id = "user-1";
});

describe("whether links can be kept on this Mac", () => {
  it("needs the store, every GitSpoke read, and a signed-in user", async () => {
    capableShell();
    expect(await canKeepLinksOnThisMac()).toBe(true);
    currentUser.id = null;
    expect(await canKeepLinksOnThisMac()).toBe(false);
  });

  it.each([
    ["outside the desktop app", undefined],
    ["on a shell without the store", { localPullRequestLinks: undefined }],
    [
      "on a shell that doesn't list the store",
      {
        getDesktopFeatures: async () => ({
          codeHosts: [{ id: "gitspoke", operations: [...GITSPOKE_READ_OPERATIONS] }],
        }),
      },
    ],
  ])("is off %s, and lists nothing", async (_label, overrides) => {
    if (overrides) capableShell(overrides);
    expect(await canKeepLinksOnThisMac()).toBe(false);
    expect(await listLocalPullRequests(SESSION)).toEqual([]);
  });

  it.each([
    ["GitSpoke reads are turned off", []],
    ["a GitSpoke read is missing", [{ id: "gitspoke", operations: ["pull_request_lookup"] }]],
  ])("keeps saved links listed and removable when %s", async (_label, codeHosts) => {
    const { localPullRequestLinks, codeHostQuery } = capableShell({
      getDesktopFeatures: async () => ({ localPullRequestLinks: true, codeHosts }),
    });
    expect(await canKeepLinksOnThisMac()).toBe(false);
    expect((await listLocalPullRequests(SESSION)).map((pr) => pr.url)).toEqual([SPOKE_PR]);
    expect(await removeLocalPullRequest(SESSION, SPOKE_PR)).toEqual([]);
    expect(localPullRequestLinks).toHaveBeenLastCalledWith(
      expect.objectContaining({ action: "remove", sessionId: SESSION, url: SPOKE_PR }),
    );
    // Linking anew still needs a shell that can read the PR.
    expect(await linkPullRequestOnThisMac(SESSION, SPOKE_PR)).toBeNull();
    expect(codeHostQuery).not.toHaveBeenCalled();
  });
});

describe("forgetting links", () => {
  it("forgets a deleted session's links for the signed-in user", async () => {
    const { localPullRequestLinks } = capableShell();
    await forgetLocalPullRequestSession(SESSION);
    expect(localPullRequestLinks).toHaveBeenCalledWith({
      action: "forget_session",
      sessionId: SESSION,
      userId: "user-1",
    });
  });

  it("forgets the server's links before signing out, on any shell with the store", async () => {
    const { localPullRequestLinks } = capableShell({
      getDesktopFeatures: async () => null,
    });
    await expect(forgetLocalPullRequestServer()).resolves.toBe(true);
    expect(localPullRequestLinks).toHaveBeenCalledWith({ action: "forget_server" });
    // A shell that couldn't save the purge says so.
    localPullRequestLinks.mockResolvedValueOnce({
      ok: false,
      error: { code: "storage_failed" },
    } as never);
    await expect(forgetLocalPullRequestServer()).resolves.toBe(false);
    // Without the store there's nothing on this Mac to forget.
    installShell(undefined);
    await expect(forgetLocalPullRequestServer()).resolves.toBe(true);
  });
});

describe("listing and unlinking", () => {
  it("lists the session's links as associations saved on this Mac, for the signed-in user", async () => {
    const { localPullRequestLinks } = capableShell();
    expect(await listLocalPullRequests(SESSION)).toEqual([
      expect.objectContaining({
        url: SPOKE_PR,
        provider: "gitspoke",
        relationship: "attached",
        saved_on_device: true,
      }),
    ]);
    expect(localPullRequestLinks).toHaveBeenCalledWith({
      action: "list",
      sessionId: SESSION,
      userId: "user-1",
    });
  });

  it("lists nothing when the shell can't answer", async () => {
    capableShell({
      localPullRequestLinks: async () => ({ ok: false, error: { code: "storage_failed" } }),
    });
    expect(await listLocalPullRequests(SESSION)).toEqual([]);
  });

  it("says so when an unlink can't be saved", async () => {
    capableShell({
      localPullRequestLinks: async () => ({ ok: false, error: { code: "storage_failed" } }),
    });
    await expect(removeLocalPullRequest(SESSION, SPOKE_PR)).rejects.toThrow("couldn't remove");
  });
});

describe("listing every session's links at once", () => {
  const byUserFeatures = async () => ({
    localPullRequestLinks: true,
    localPullRequestLinksByUser: true,
    codeHosts: [{ id: "gitspoke", operations: [...GITSPOKE_READ_OPERATIONS] }],
  });

  it("groups the user's links by session in one request", async () => {
    const localPullRequestLinks = vi.fn(async () => ({
      ok: true,
      data: [
        { ...LINK, session_id: SESSION },
        { ...LINK, number: 7, url: `${SPOKE_PR.slice(0, -8)}7`, session_id: "other" },
        // Without a session it can't be matched to one.
        { ...LINK, number: 8 },
      ],
    }));
    capableShell({ getDesktopFeatures: byUserFeatures, localPullRequestLinks });
    const bySession = await listLocalPullRequestsByUser();
    expect([...bySession.keys()]).toEqual([SESSION, "other"]);
    expect(bySession.get(SESSION)?.[0]).toMatchObject({ url: SPOKE_PR, saved_on_device: true });
    expect(localPullRequestLinks).toHaveBeenCalledExactlyOnceWith({
      action: "list_user",
      userId: "user-1",
    });
  });

  it("reads nothing from a shell without list_user, or with no user signed in", async () => {
    const { localPullRequestLinks } = capableShell();
    expect((await listLocalPullRequestsByUser()).size).toBe(0);
    currentUser.id = null;
    capableShell({ getDesktopFeatures: byUserFeatures, localPullRequestLinks });
    expect((await listLocalPullRequestsByUser()).size).toBe(0);
    expect(localPullRequestLinks).not.toHaveBeenCalled();
  });
});

describe("localOnlyPullRequests", () => {
  it("keeps the local links the server doesn't already hold, matched by identity", async () => {
    capableShell();
    const [local] = await listLocalPullRequests(SESSION);
    expect(localOnlyPullRequests([], [local])).toEqual([local]);
    expect(
      localOnlyPullRequests(
        [
          {
            host: local.host.toUpperCase(),
            repository: local.repository.toUpperCase(),
            number: local.number,
          },
        ],
        [local],
      ),
    ).toEqual([]);
  });
});

describe("linkPullRequestOnThisMac", () => {
  it("confirms this Mac can read the PR, then keeps its canonical link", async () => {
    const { codeHostQuery, localPullRequestLinks } = capableShell();
    const result = await linkPullRequestOnThisMac(SESSION, `${SPOKE_PR}/?w=1`);
    expect(result?.url).toBe(SPOKE_PR);
    expect(result?.links.map((pr) => pr.url)).toEqual([SPOKE_PR]);
    expect(codeHostQuery).toHaveBeenCalledWith({
      provider: "gitspoke",
      host: "2.spoke.example.com",
      operation: "pull_request_lookup",
      params: { repository: "example-org/universe", number: 20002346 },
    });
    expect(localPullRequestLinks).toHaveBeenCalledWith(
      expect.objectContaining({ action: "add", url: SPOKE_PR, sessionId: SESSION }),
    );
  });

  it("keeps nothing when this Mac can't read the PR, and says why", async () => {
    const { localPullRequestLinks } = capableShell({
      codeHostQuery: async () => ({
        ok: false,
        error: { code: "no_access", message: "GitSpoke didn't let this Mac's account read that." },
      }),
    });
    await expect(linkPullRequestOnThisMac(SESSION, SPOKE_PR)).rejects.toThrow("didn't let");
    expect(localPullRequestLinks).not.toHaveBeenCalled();
  });

  it("leaves other links, and shells that can't keep them, to the host", async () => {
    const { codeHostQuery } = capableShell();
    expect(await linkPullRequestOnThisMac(SESSION, "https://github.com/o/r/pull/1")).toBeNull();
    installShell(undefined);
    expect(await linkPullRequestOnThisMac(SESSION, SPOKE_PR)).toBeNull();
    expect(codeHostQuery).not.toHaveBeenCalled();
  });
});

describe("merging sources", () => {
  const stored = {
    url: "https://github.com/o/r/pull/1",
    host: "github.com",
    repository: "o/r",
    number: 1,
    relationship: "attached" as const,
  };
  const local = {
    url: SPOKE_PR,
    host: "2.spoke.example.com",
    repository: "example-org/universe",
    number: 20002346,
    relationship: "attached" as const,
    saved_on_device: true,
  };

  it("adds local links after the server's, once per PR", () => {
    expect(withLocalPullRequests([stored], [local]).map((pr) => pr.url)).toEqual([
      stored.url,
      SPOKE_PR,
    ]);
    const sameOnServer = {
      ...local,
      host: "2.Spoke.example.com",
      saved_on_device: undefined,
    };
    expect(withLocalPullRequests([sameOnServer], [local])).toEqual([sameOnServer]);
  });

  it("tells a local link from a server one", () => {
    expect(isLocalPullRequest(SPOKE_PR, [stored, local])).toBe(true);
    expect(isLocalPullRequest(stored.url, [stored, local])).toBe(false);
    expect(isLocalPullRequest(SPOKE_PR, undefined)).toBe(false);
  });
});
