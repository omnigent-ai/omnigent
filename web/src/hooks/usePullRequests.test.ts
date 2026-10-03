// Tests for the pure helpers in usePullRequests — the 404 → reason classifier that
// steers an outdated host to the "update your host" panel state, the payload
// normalizer (and that the info fetch applies it), and the panel's
// poll-interval decision.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  computePullRequestPollInterval,
  fetchPullRequestInfo,
  normalizePullRequestInfo,
  pullRequestNotFoundReason,
  type PullRequestAccount,
  type PullRequestAuth,
  type PullRequestCapabilities,
  type PullRequestChecks,
  type PullRequestInfo,
} from "@/hooks/usePullRequests";

const FORGE_DISPLAY = {
  id: "example_forge",
  display_name: "Example Forge",
  request_name: "pull request",
  number_prefix: "!",
};

describe("pullRequestNotFoundReason", () => {
  it("flags an outdated host from its 'resource not found' message", () => {
    // The exact shape an old runner's generic resource lookup returns.
    expect(pullRequestNotFoundReason("Resource 'github' not found")).toBe("host_outdated");
    // Case-insensitive, tolerant of quoting.
    expect(pullRequestNotFoundReason("resource github not found")).toBe("host_outdated");
  });

  it("treats every other 404 as a generic no-workspace reason", () => {
    expect(pullRequestNotFoundReason("workspace directory does not exist on host")).toBe(
      "no_os_env",
    );
    expect(pullRequestNotFoundReason("Resource 'terminal' not found")).toBe("no_os_env");
    expect(pullRequestNotFoundReason(undefined)).toBe("no_os_env");
    expect(pullRequestNotFoundReason("")).toBe("no_os_env");
  });
});

describe("normalizePullRequestInfo", () => {
  const accounts: PullRequestAccount[] = [
    { login: "personal", active: true, state: "success", host: "github.com" },
    { login: "work", active: false, state: "success", host: "github.com" },
  ];
  const githubCapabilities: PullRequestCapabilities = {
    account_switching: true,
    base_remote_selection: true,
    line_counts: true,
    linked_pr_diff: true,
  };

  it("fills GitHub defaults from a legacy payload and keeps the legacy fields", () => {
    const legacy: PullRequestInfo = {
      object: "session.github.info",
      available: true,
      gh_available: false,
      authenticated: false,
      accounts,
      selected_account: "work",
    };
    expect(normalizePullRequestInfo(legacy)).toEqual({
      ...legacy,
      provider: "github",
      auth: {
        authenticated: false,
        hint: null,
        cli: { name: "gh", available: false },
        accounts,
        selected_account: "work",
      },
      capabilities: githubCapabilities,
    });
  });

  it("leaves the CLI and accounts empty when the legacy fields are absent", () => {
    const info = normalizePullRequestInfo({
      object: "session.github.info",
      available: false,
      reason: "no_os_env",
    });
    expect(info.auth).toEqual({
      authenticated: true,
      hint: null,
      cli: null,
      accounts: null,
      selected_account: null,
    });
  });

  it("treats a legacy payload without gh as signed out, whatever `authenticated` says", () => {
    const info = (over: Partial<PullRequestInfo>) =>
      normalizePullRequestInfo({ object: "session.github.info", available: true, ...over }).auth;
    expect(info({ gh_available: false, authenticated: true })).toMatchObject({
      authenticated: false,
      cli: { name: "gh", available: false },
    });
    expect(info({ gh_available: false })).toMatchObject({ authenticated: false });
    expect(info({ gh_available: true, authenticated: true })).toMatchObject({
      authenticated: true,
    });
    expect(info({ gh_available: true })).toMatchObject({ authenticated: true });
  });

  it("keeps an explicit null provider and turns an absent one into GitHub", () => {
    const remote: PullRequestInfo = {
      object: "session.github.info",
      available: false,
      reason: "unsupported_remote",
      remote_host: "gitlab.com",
      provider: null,
    };
    expect(normalizePullRequestInfo(remote).provider).toBeNull();
    const noProvider: PullRequestInfo = { ...remote };
    delete noProvider.provider;
    expect(normalizePullRequestInfo(noProvider).provider).toBe("github");
    // Idempotent for the null case too, since readers may normalize twice.
    expect(normalizePullRequestInfo(normalizePullRequestInfo(remote)).provider).toBeNull();
  });

  it("prefers auth over the legacy fields when a host sends both", () => {
    const auth: PullRequestAuth = {
      authenticated: true,
      hint: null,
      cli: { name: "gh", available: true },
      accounts,
      selected_account: "personal",
    };
    const info = normalizePullRequestInfo({
      object: "session.github.info",
      available: true,
      provider: "github",
      auth,
      gh_available: false,
      authenticated: false,
      accounts: [],
      selected_account: "work",
    });
    expect(info.auth).toEqual(auth);
    expect(info.gh_available).toBe(false);
  });

  it("passes a non-GitHub payload through unchanged", () => {
    const forge: PullRequestInfo = {
      object: "session.github.info",
      available: true,
      provider: "example_forge",
      provider_display: FORGE_DISPLAY,
      auth: {
        authenticated: true,
        hint: "Run forge login on the host.",
        cli: { name: "forge", available: true },
        accounts: null,
        selected_account: null,
      },
      capabilities: {
        account_switching: false,
        base_remote_selection: false,
        line_counts: false,
        linked_pr_diff: false,
      },
      repo: { name_with_owner: "org/project/repo" },
    };
    expect(normalizePullRequestInfo(forge)).toEqual(forge);
  });
});

describe("fetchPullRequestInfo", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("normalizes the host's payload and the synthesized 404 payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({ object: "session.github.info", available: true, gh_available: true }),
            { status: 200 },
          ),
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ error: { message: "Resource 'github' not found" } }), {
            status: 404,
          }),
        ),
    );
    await expect(fetchPullRequestInfo("conv")).resolves.toMatchObject({
      provider: "github",
      auth: { cli: { name: "gh", available: true } },
    });
    // The 404 says nothing about a provider, so the synthesized payload names none.
    await expect(fetchPullRequestInfo("conv")).resolves.toMatchObject({
      reason: "host_outdated",
      provider: null,
      auth: { authenticated: true, cli: null },
    });
  });
});

describe("computePullRequestPollInterval", () => {
  const checks = (over: Partial<PullRequestChecks> = {}): PullRequestChecks => ({
    passing: 0,
    failing: 0,
    pending: 0,
    total: 0,
    runs: [],
    ...over,
  });
  // A fully-resolved, ready session: git repo + gh + auth + repo + open PR.
  const ready = (over: Partial<PullRequestInfo> = {}): PullRequestInfo => ({
    object: "session.github.info",
    available: true,
    gh_available: true,
    authenticated: true,
    branch: "feature",
    repo: { name_with_owner: "o/r" },
    base_ref: "main",
    pr: {
      number: 1,
      title: "t",
      state: "OPEN",
      url: "u",
      is_draft: false,
      author: null,
      base_ref: "main",
      head_ref: "feature",
      checks: checks({ total: 2, passing: 2 }),
    },
    ...over,
  });

  it("polls setup/availability states the user fixes outside the app", () => {
    // No info yet (initial error / transient failure with no cache).
    expect(computePullRequestPollInterval(undefined)).toBe(5_000);
    // Not a git repo / no workspace / outdated host.
    expect(computePullRequestPollInterval(ready({ available: false, pr: null }))).toBe(5_000);
    // gh not installed.
    expect(computePullRequestPollInterval(ready({ gh_available: false, pr: null }))).toBe(5_000);
    // Not authenticated (the `gh auth switch` / `gh auth login` prompt).
    expect(computePullRequestPollInterval(ready({ authenticated: false, pr: null }))).toBe(5_000);
    // Repo unresolved (no upstream).
    expect(computePullRequestPollInterval(ready({ repo: null, pr: null }))).toBe(5_000);
    expect(
      computePullRequestPollInterval(ready({ repo: { name_with_owner: null }, pr: null })),
    ).toBe(5_000);
  });

  it("polls while waiting for a PR and while an open PR's checks are unsettled", () => {
    // Set up, no PR yet — waiting for one to appear.
    expect(computePullRequestPollInterval(ready({ pr: null }))).toBe(5_000);
    // Open PR, checks running.
    expect(
      computePullRequestPollInterval(
        ready({ pr: { ...ready().pr!, checks: checks({ pending: 1 }) } }),
      ),
    ).toBe(5_000);
    // Open PR, checks not registered yet (total 0).
    expect(
      computePullRequestPollInterval(ready({ pr: { ...ready().pr!, checks: checks() } })),
    ).toBe(5_000);
  });

  it("reads the sign-in state from auth, else from the legacy fields", () => {
    const auth = (over: Partial<PullRequestAuth> = {}): PullRequestAuth => ({
      authenticated: true,
      hint: null,
      cli: { name: "forge", available: true },
      accounts: null,
      selected_account: null,
      ...over,
    });
    // A settled PR still polls while signed out, with or without the provider CLI.
    expect(
      computePullRequestPollInterval(
        ready({ auth: auth({ authenticated: false, cli: { name: "forge", available: false } }) }),
      ),
    ).toBe(5_000);
    expect(computePullRequestPollInterval(ready({ auth: auth({ authenticated: false }) }))).toBe(
      5_000,
    );
    expect(computePullRequestPollInterval(ready({ auth: auth() }))).toBe(false);
    // A token signs in without the CLI, so a missing one is not a setup state.
    expect(
      computePullRequestPollInterval(
        ready({ auth: auth({ cli: { name: "forge", available: false } }) }),
      ),
    ).toBe(false);
    // A legacy payload without `auth` maps through the normalizer.
    expect(computePullRequestPollInterval(ready({ gh_available: false }))).toBe(5_000);
  });

  it("keeps polling incomplete results even when the request and checks are settled", () => {
    const merged = { ...ready().pr!, state: "MERGED" };
    expect(
      computePullRequestPollInterval(ready({ pr: merged, warnings: ["Lookup failed."] })),
    ).toBe(5_000);
    expect(
      computePullRequestPollInterval(
        ready({ pr: { ...merged, checks: { ...merged.checks, partial: true } } }),
      ),
    ).toBe(5_000);
    expect(
      computePullRequestPollInterval(ready({ pr: { ...merged, comments_partial: true } })),
    ).toBe(5_000);
  });

  it("rests once there is nothing left to watch", () => {
    // Open PR, all checks settled.
    expect(computePullRequestPollInterval(ready())).toBe(false);
    // Merged / closed PR — terminal.
    expect(computePullRequestPollInterval(ready({ pr: { ...ready().pr!, state: "MERGED" } }))).toBe(
      false,
    );
    expect(computePullRequestPollInterval(ready({ pr: { ...ready().pr!, state: "CLOSED" } }))).toBe(
      false,
    );
  });
});
