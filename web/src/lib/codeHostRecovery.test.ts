import { describe, expect, it } from "vitest";
import {
  githubRecovery,
  githubRecoveryCopy,
  githubSignInCommand,
  signInHostEvidence,
  unsupportedRemoteRecovery,
} from "@/lib/codeHostRecovery";
import {
  normalizePullRequestInfo,
  type PullRequestAccount,
  type PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

function account(login: string, state: string | null, host = "github.com"): PullRequestAccount {
  return { login, state, host, active: false };
}

function info(
  overrides: Omit<Partial<PullRequestInfo>, "accounts"> & {
    accounts?: PullRequestAccount[] | null;
  },
) {
  const { accounts = null, authenticated = false, ...rest } = overrides;
  return normalizePullRequestInfo({
    object: "session.github.info",
    available: true,
    provider: "github",
    repo: null,
    auth: {
      authenticated,
      hint: null,
      cli: { name: "gh", available: true },
      accounts,
      selected_account: null,
    },
    ...rest,
  });
}

describe("githubRecovery", () => {
  it("doesn't claim signed-out from an empty account list", () => {
    expect(githubRecovery(info({ accounts: [] }))).toEqual({
      kind: "not-signed-in-or-unchecked",
      signInHost: null,
    });
    // An older gh lists no accounts even when signed in.
    expect(githubRecovery(info({ accounts: [], authenticated: true }))).toEqual({
      kind: "repo-unreachable",
    });
  });

  it("reports each account's check as gh observed it, never as expired", () => {
    const recovery = githubRecovery(
      info({ accounts: [account("a", "error"), account("b", "timeout"), account("c", null)] }),
    );
    expect(recovery).toEqual({
      kind: "sign-in-not-working",
      signInHost: null,
      accounts: [
        { login: "a", host: "github.com", check: "failed" },
        { login: "b", host: "github.com", check: "timed-out" },
        { login: "c", host: "github.com", check: "unchecked" },
      ],
    });
    expect(JSON.stringify(githubRecoveryCopy(recovery!))).not.toMatch(/expired/i);
  });

  it("reads a working account with no repo as a failed check, not as missing access", () => {
    const recovery = githubRecovery(
      info({
        authenticated: true,
        accounts: [account("work", "success"), account("old", "error")],
      }),
    )!;
    expect(recovery).toEqual({
      kind: "repo-check-failed",
      accounts: [{ login: "work", host: "github.com", check: "passed" }],
      canPickAccount: true,
    });
    // The host doesn't say why the repo didn't open; a network failure reads the same.
    for (const surface of [undefined, { kind: "desktop" as const, device: "this Mac" }]) {
      const { title, hint } = githubRecoveryCopy(recovery, surface);
      expect(title).toBe("Couldn’t open this repository");
      expect(`${title} ${hint}`).not.toMatch(/can’t see|can't see|can’t reach/i);
      expect(hint).toMatch(/may not have access, or GitHub didn’t answer/);
    }
  });

  it("reads every account check timing out as a timed-out check, not a broken sign-in", () => {
    const recovery = githubRecovery(
      info({ accounts: [account("a", "timeout"), account("b", "timeout", "ghe.example.com")] }),
    )!;
    expect(recovery).toEqual({ kind: "check-timed-out" });
    for (const surface of [undefined, { kind: "desktop" as const, device: "this Mac" }]) {
      const { title, hint } = githubRecoveryCopy(recovery, surface);
      expect(title).toBe("Checking GitHub timed out");
      expect(`${title} ${hint}`).not.toMatch(/sign-in|sign in|access/i);
    }
  });

  it("keeps an explicit failure beside a timeout as a sign-in problem", () => {
    expect(
      githubRecovery(info({ accounts: [account("a", "error"), account("b", "timeout")] }))?.kind,
    ).toBe("sign-in-not-working");
  });

  it("reads a working account beside a timed-out one as a failed repository check", () => {
    expect(
      githubRecovery(
        info({ authenticated: true, accounts: [account("a", "timeout"), account("b", "success")] }),
      ),
    ).toEqual({
      kind: "repo-check-failed",
      accounts: [{ login: "b", host: "github.com", check: "passed" }],
      canPickAccount: true,
    });
  });

  it("offers picking another account only where the selector can show", () => {
    const one = githubRecovery(info({ authenticated: true, accounts: [account("w", "success")] }))!;
    expect(one).toMatchObject({ kind: "repo-check-failed", canPickAccount: false });
    expect(githubRecoveryCopy(one).hint).not.toMatch(/Pick another account/);
    const locked = githubRecovery(
      info({
        authenticated: true,
        accounts: [account("a", "success"), account("b", "success")],
        capabilities: {
          account_switching: false,
          base_remote_selection: false,
          line_counts: true,
          linked_pr_diff: true,
        },
      }),
    )!;
    expect(locked).toMatchObject({ kind: "repo-check-failed", canPickAccount: false });
    expect(githubRecoveryCopy(locked).hint).not.toMatch(/Pick another account/);
  });

  it("keeps the provider's own guidance where the payload proves nothing new", () => {
    // The repo resolved.
    expect(
      githubRecovery(info({ authenticated: true, accounts: [], repo: { name_with_owner: "o/r" } })),
    ).toBeNull();
    // Another provider serves the workspace.
    expect(githubRecovery(info({ provider: "gitlab", accounts: [] }))).toBeNull();
  });

  it("claims nothing about sign-in for a host too old to list accounts", () => {
    const recovery = githubRecovery(info({ authenticated: true, accounts: null }))!;
    expect(recovery).toEqual({ kind: "unknown" });
    expect(githubRecoveryCopy(recovery).title).toBe("Can’t reach the upstream repo");
    expect(githubRecoveryCopy(recovery, { kind: "desktop", device: "this Mac" })).toEqual({
      title: "Can’t reach this repository",
      hint: "The session’s host couldn’t open this repository. Omnigent can’t load pull requests with this Mac’s GitHub account yet.",
    });
  });

  it("reads an explicit sign-out from an older host as not signed in or unchecked", () => {
    expect(githubRecovery(info({ accounts: null }))?.kind).toBe("not-signed-in-or-unchecked");
  });
});

describe("signInHostEvidence", () => {
  const pr = (url: string) => ({
    url,
    host: "",
    repository: "o/r",
    number: 1,
    relationship: "attached" as const,
  });

  it("uses the selected PR's host, then a host every tracked PR shares", () => {
    expect(
      signInHostEvidence(info({ selected_pr_url: "https://GHE.example.com/o/r/pull/1" })),
    ).toBe("ghe.example.com");
    expect(signInHostEvidence(info({ prs: [pr("https://ghe.example.com/o/r/pull/1")] }))).toBe(
      "ghe.example.com",
    );
  });

  it("never infers a host from accounts or from PRs on several hosts", () => {
    expect(
      signInHostEvidence(info({ accounts: [account("a", "error", "ghe.example.com")] })),
    ).toBeNull();
    expect(
      signInHostEvidence(
        info({
          prs: [pr("https://github.com/o/r/pull/1"), pr("https://ghe.example.com/o/r/pull/2")],
        }),
      ),
    ).toBeNull();
    expect(
      signInHostEvidence(info({ selected_pr_url: "http://ghe.example.com/o/r/pull/1" })),
    ).toBeNull();
  });

  it("names the evidenced host in the sign-in command", () => {
    const recovery = githubRecovery(
      info({ accounts: [], selected_pr_url: "https://ghe.example.com/o/r/pull/1" }),
    )!;
    expect(githubRecoveryCopy(recovery).hint).toContain(
      "`gh auth login --hostname ghe.example.com`",
    );
  });
});

describe("githubSignInCommand", () => {
  it("names the host only when it isn't github.com", () => {
    expect(githubSignInCommand(null)).toBe("gh auth login");
    expect(githubSignInCommand("github.com")).toBe("gh auth login");
    expect(githubSignInCommand("ghe.example.com")).toBe("gh auth login --hostname ghe.example.com");
  });
});

describe("unsupportedRemoteRecovery", () => {
  it.each([
    ["3.host.example.com", "named-host-remote"],
    ["12.host.example.com", "named-host-remote"],
    ["0.host.example.com", "other-host-remote"],
    ["02.host.example.com", "other-host-remote"],
    ["devices.example.com", "other-host-remote"],
    ["git.example.com", "other-host-remote"],
  ])("classifies %s as %s", (host, kind) => {
    expect(unsupportedRemoteRecovery(host).kind).toBe(kind);
  });

  it("treats a missing or malformed host as unknown", () => {
    expect(unsupportedRemoteRecovery(undefined)).toEqual({ kind: "unknown-remote" });
    expect(unsupportedRemoteRecovery("not a host")).toEqual({ kind: "unknown-remote" });
  });
});

describe("githubRecoveryCopy on desktop", () => {
  const desktop = { kind: "desktop" as const, device: "this Mac" };
  const recoveries = [
    githubRecovery(info({ accounts: [] }))!,
    githubRecovery(info({ accounts: [account("a", "error")] }))!,
    githubRecovery(info({ authenticated: true, accounts: [account("a", "success")] }))!,
    githubRecovery(info({ authenticated: true, accounts: [] }))!,
    githubRecovery(info({ authenticated: true, accounts: null }))!,
    githubRecovery(info({ accounts: [], selected_pr_url: "https://ghe.example.com/o/r/pull/1" }))!,
  ];

  it("never sends desktop users to the host's GitHub CLI", () => {
    for (const recovery of recoveries) {
      const { title, hint } = githubRecoveryCopy(recovery, desktop);
      expect(`${title} ${hint}`, recovery.kind).not.toMatch(/gh auth|`/);
    }
  });

  it("says plainly what's wrong and that this Mac's account can't be used yet", () => {
    expect(githubRecoveryCopy(recoveries[0], desktop)).toEqual({
      title: "Can’t load this session’s pull requests",
      hint: "The GitHub CLI on the session’s host has no working sign-in, or Omnigent couldn’t check it. Omnigent can’t load pull requests with this Mac’s GitHub account yet.",
    });
  });

  it("keeps the host sign-in command on the web", () => {
    expect(githubRecoveryCopy(recoveries[0]).hint).toContain("`gh auth login`");
  });
});
