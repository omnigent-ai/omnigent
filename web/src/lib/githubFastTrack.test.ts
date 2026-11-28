import { afterEach, describe, expect, it, vi } from "vitest";

import {
  checkGitHubFastTrackAccount,
  isGitHubFastTrackAvailable,
  pullRequestFastTrackAccount,
  queryGitHubFastTrack,
} from "./githubFastTrack";

const ACCOUNT = { host: "github.com", login: "octo_data" };

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

afterEach(() => installShell(undefined));

describe("isGitHubFastTrackAvailable", () => {
  it("is off outside the desktop shell", async () => {
    expect(await isGitHubFastTrackAvailable()).toBe(false);
  });

  it("is off in an older shell without the bridge method", async () => {
    installShell({ getDesktopFeatures: async () => ({ githubFastTrack: true }) });
    expect(await isGitHubFastTrackAvailable()).toBe(false);
  });

  it.each([
    ["no features", undefined],
    ["null features", async () => null],
    ["features without the field", async () => ({})],
    ["the feature off", async () => ({ githubFastTrack: false })],
  ])("is off for a shell reporting %s", async (_label, getDesktopFeatures) => {
    const githubQuery = vi.fn();
    installShell({ githubQuery, ...(getDesktopFeatures ? { getDesktopFeatures } : {}) });
    expect(await isGitHubFastTrackAvailable()).toBe(false);
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("is on when the shell has the bridge and reports the feature", async () => {
    installShell({
      githubQuery: vi.fn(),
      getDesktopFeatures: async () => ({ githubFastTrack: true }),
    });
    expect(await isGitHubFastTrackAvailable()).toBe(true);
  });
});

describe("pullRequestFastTrackAccount", () => {
  it("uses exactly the host's selected login for a GitHub PR", () => {
    expect(
      pullRequestFastTrackAccount({
        provider: "github",
        prHost: "GitHub.com",
        selectedAccount: "octo_data",
      }),
    ).toEqual(ACCOUNT);
  });

  it("has no account when the host doesn't report one or the PR isn't on GitHub", () => {
    for (const input of [
      { provider: "github", prHost: "github.com", selectedAccount: null },
      { provider: "github", prHost: "github.com", selectedAccount: "" },
      { provider: "github", prHost: null, selectedAccount: "octo_data" },
      { provider: "gitlab", prHost: "gitlab.com", selectedAccount: "octo_data" },
      { provider: null, prHost: "github.com", selectedAccount: "octo_data" },
    ]) {
      expect(pullRequestFastTrackAccount(input)).toBeNull();
    }
  });
});

describe("queryGitHubFastTrack", () => {
  it("answers unsupported without asking an older or partial shell", async () => {
    const githubQuery = vi.fn();
    installShell({ githubQuery, getDesktopFeatures: async () => ({}) });
    const result = await queryGitHubFastTrack("account_status", ACCOUNT);
    expect(result).toMatchObject({ ok: false, error: { code: "unsupported" } });
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("passes the operation and account to a capable shell", async () => {
    const githubQuery = vi.fn().mockResolvedValue({ ok: true, data: { login: "Octo_Data" } });
    installShell({ githubQuery, getDesktopFeatures: async () => ({ githubFastTrack: true }) });
    expect(await queryGitHubFastTrack("account_status", ACCOUNT)).toEqual({
      ok: true,
      data: { login: "Octo_Data" },
    });
    expect(githubQuery).toHaveBeenCalledWith("account_status", ACCOUNT, undefined);
  });

  it.each([
    ["rejects", () => Promise.reject(new Error("No handler registered"))],
    ["resolves nothing", () => Promise.resolve(undefined)],
  ])("treats a bridge that %s as unsupported", async (_label, githubQuery) => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    installShell({ githubQuery, getDesktopFeatures: async () => ({ githubFastTrack: true }) });
    expect(await queryGitHubFastTrack("account_status", ACCOUNT)).toMatchObject({
      ok: false,
      error: { code: "unsupported" },
    });
    warn.mockRestore();
  });
});

describe("checkGitHubFastTrackAccount", () => {
  it("is unsupported without an account, without asking the shell", async () => {
    const githubQuery = vi.fn();
    installShell({ githubQuery, getDesktopFeatures: async () => ({ githubFastTrack: true }) });
    expect(await checkGitHubFastTrackAccount(null)).toEqual({ status: "unsupported" });
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("is ready with gh's spelling of the login", async () => {
    installShell({
      githubQuery: vi.fn().mockResolvedValue({ ok: true, data: { login: "Octo_Data" } }),
      getDesktopFeatures: async () => ({ githubFastTrack: true }),
    });
    expect(await checkGitHubFastTrackAccount(ACCOUNT)).toEqual({
      status: "ready",
      account: { host: "github.com", login: "Octo_Data" },
    });
  });

  it("is unavailable, with the shell's message, when the login isn't signed in here", async () => {
    const error = {
      code: "account_missing",
      message: "That GitHub account isn't signed in on this Mac. Run gh auth login.",
    };
    installShell({
      githubQuery: vi.fn().mockResolvedValue({ ok: false, error }),
      getDesktopFeatures: async () => ({ githubFastTrack: true }),
    });
    expect(await checkGitHubFastTrackAccount(ACCOUNT)).toEqual({ status: "unavailable", error });
  });

  it("is unsupported when the shell turns out not to support it", async () => {
    installShell({
      githubQuery: vi.fn().mockResolvedValue({
        ok: false,
        error: { code: "unsupported", message: "off" },
      }),
      getDesktopFeatures: async () => ({ githubFastTrack: true }),
    });
    expect(await checkGitHubFastTrackAccount(ACCOUNT)).toEqual({ status: "unsupported" });
  });
});

describe("operation negotiation", () => {
  const COMMIT_OPERATIONS = ["pull_request_commits", "commit_diff"];

  it("treats a shell that lists no operations as running only account_status", async () => {
    const githubQuery = vi.fn().mockResolvedValue({ ok: true, data: {} });
    installShell({ githubQuery, getDesktopFeatures: async () => ({ githubFastTrack: true }) });
    expect(await isGitHubFastTrackAvailable()).toBe(true);
    expect(await isGitHubFastTrackAvailable(["account_status"])).toBe(true);
    expect(await isGitHubFastTrackAvailable(COMMIT_OPERATIONS)).toBe(false);
    expect(await queryGitHubFastTrack("commit_diff", ACCOUNT)).toMatchObject({
      ok: false,
      error: { code: "unsupported" },
    });
    expect(await checkGitHubFastTrackAccount(ACCOUNT, COMMIT_OPERATIONS)).toEqual({
      status: "unsupported",
    });
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("requires every needed operation in the shell's list", async () => {
    const githubQuery = vi.fn().mockResolvedValue({ ok: true, data: { login: "octo_data" } });
    installShell({
      githubQuery,
      getDesktopFeatures: async () => ({
        githubFastTrack: true,
        githubFastTrackOperations: ["account_status", "pull_request_commits"],
      }),
    });
    expect(await isGitHubFastTrackAvailable(["pull_request_commits"])).toBe(true);
    expect(await isGitHubFastTrackAvailable(COMMIT_OPERATIONS)).toBe(false);
    installShell({
      githubQuery,
      getDesktopFeatures: async () => ({
        githubFastTrack: true,
        githubFastTrackOperations: ["account_status", ...COMMIT_OPERATIONS],
      }),
    });
    expect(await checkGitHubFastTrackAccount(ACCOUNT, COMMIT_OPERATIONS)).toEqual({
      status: "ready",
      account: ACCOUNT,
    });
  });
});
