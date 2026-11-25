import { describe, expect, it } from "vitest";

import type { MacWriteUnavailableReason } from "@/lib/desktopPullRequestWrites";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { gitProviderCopy } from "@/lib/gitProviders";
import { commentWritesRecovery } from "./commentWritesRecovery";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const GITHUB: CodeHostPullRequest = {
  provider: "github",
  host: "github.com",
  repository: "acme/app",
  number: 42,
  url: "https://github.com/acme/app/pull/42",
};
const ENTERPRISE: CodeHostPullRequest = {
  ...GITHUB,
  host: "ghe.example.com",
  url: "https://ghe.example.com/acme/app/pull/42",
};
const SPOKE: CodeHostPullRequest = {
  provider: "gitspoke",
  host: "2.spoke.example.com",
  repository: "example-org/universe",
  number: 7,
  url: "https://2.spoke.example.com/example-org/universe/pull/7",
};

const mac = (reason: MacWriteUnavailableReason, target = GITHUB) => ({
  kind: "mac" as const,
  unavailable: { reason, target, reader: null },
});

describe("commentWritesRecovery", () => {
  it("has nothing to say when the box can post", () => {
    expect(commentWritesRecovery({ block: null, refusal: null, signInAvailable: true })).toBeNull();
  });

  it("offers a sign-in for the PR's own Enterprise host, with the Terminal steps", () => {
    const recovery = commentWritesRecovery({
      block: mac("no_account", ENTERPRISE),
      refusal: null,
      signInAvailable: true,
    });
    expect(recovery).toMatchObject({
      reason: "no_account",
      blocksPosting: true,
      signInHost: "ghe.example.com",
      terminalCommand: "gh auth login --hostname ghe.example.com",
      checkAgain: true,
      openLabel: "Open on GitHub",
    });
    expect(recovery!.message).toContain("ghe.example.com");
  });

  it("shows no sign-in button where the shell can't sign in, only the Terminal steps", () => {
    expect(
      commentWritesRecovery({ block: mac("no_account"), refusal: null, signInAvailable: false }),
    ).toMatchObject({ signInHost: null, terminalCommand: "gh auth login", checkAgain: true });
  });

  it("turns Settings' Off back on rather than sending the user to a sign-in list", () => {
    expect(
      commentWritesRecovery({ block: mac("writes_off"), refusal: null, signInAvailable: true }),
    ).toMatchObject({ turnOnMacWrites: true, signInHost: null, terminalCommand: null });
  });

  it.each(["release_off", "shell_too_old", "page_unsupported"] as const)(
    "%s offers only GitHub, with nothing a click here could fix",
    (reason) => {
      expect(
        commentWritesRecovery({ block: mac(reason), refusal: null, signInAvailable: true }),
      ).toMatchObject({
        reason,
        blocksPosting: true,
        signInHost: null,
        terminalCommand: null,
        turnOnMacWrites: false,
        checkAgain: false,
        openLabel: "Open on GitHub",
      });
    },
  );

  it("says a shell that doesn't offer writes can't tell why, and lets the user check again", () => {
    const recovery = commentWritesRecovery({
      block: mac("shell_writes_unavailable"),
      refusal: null,
      signInAvailable: true,
    });
    expect(recovery).toMatchObject({ checkAgain: true, signInHost: null });
    expect(recovery!.message).toMatch(/may have turned it off, or .* aren’t available/);
  });

  it("sends a GitSpoke PR to GitSpoke, never to a GitHub sign-in", () => {
    expect(
      commentWritesRecovery({
        block: mac("provider_unsupported", SPOKE),
        refusal: null,
        signInAvailable: true,
      }),
    ).toMatchObject({ signInHost: null, terminalCommand: null, openLabel: "Open on GitSpoke" });
  });

  it("explains a host that can't post, and offers this Mac only where it could read it", () => {
    const block = {
      kind: "host" as const,
      provider: gitProviderCopy("github"),
      canReadOnMac: false,
    };
    expect(commentWritesRecovery({ block, refusal: null, signInAvailable: true })).toMatchObject({
      reason: "host_cannot_comment",
      readOnThisMac: false,
      blocksPosting: true,
    });
    expect(
      commentWritesRecovery({
        block: { ...block, canReadOnMac: true },
        refusal: null,
        signInAvailable: true,
      }),
    ).toMatchObject({ readOnThisMac: true });
  });

  it("names a GitLab merge request's own provider, and opens an unnamed one in the browser", () => {
    const gitlab = gitProviderCopy("gitlab", null, {
      id: "gitlab",
      display_name: "GitLab",
      request_name: "merge request",
      number_prefix: "!",
    });
    const recovery = commentWritesRecovery({
      block: { kind: "host", provider: gitlab, canReadOnMac: false },
      refusal: null,
      signInAvailable: true,
    });
    expect(recovery).toMatchObject({ openLabel: "Open on GitLab", signInHost: null });
    expect(recovery!.message).toBe(
      "The session’s machine can’t post comments on this merge request.",
    );
    expect(
      commentWritesRecovery({
        block: { kind: "host", provider: gitProviderCopy(null), canReadOnMac: false },
        refusal: null,
        signInAvailable: true,
      })!.openLabel,
    ).toBe("Open in Browser");
  });

  it.each(["signed_out", "account_missing", "account_invalid"])(
    "after this Mac refused a post with %s, offers a sign-in to its host without blocking a retry",
    (code) => {
      expect(
        commentWritesRecovery({
          block: null,
          refusal: { code, host: "ghe.example.com" },
          signInAvailable: true,
        }),
      ).toMatchObject({
        reason: code,
        blocksPosting: false,
        signInHost: "ghe.example.com",
        terminalCommand: "gh auth login --hostname ghe.example.com",
      });
    },
  );

  it.each([
    ["the flag off", mac("release_off")],
    ["Settings Off", mac("writes_off")],
    [
      "a host that can't post",
      { kind: "host" as const, provider: gitProviderCopy("github"), canReadOnMac: false },
    ],
  ])("lets %s, after a refused sign-in, block posting and lead the notice", (_label, block) => {
    const recovery = commentWritesRecovery({
      block,
      refusal: { code: "account_invalid", host: "github.com" },
      signInAvailable: true,
    });
    expect(recovery).toMatchObject({ blocksPosting: true });
    expect(recovery!.reason).not.toBe("account_invalid");
    expect(recovery!.message).not.toContain("again, then post your draft");
  });

  it.each(["forbidden", "not_found", "rejected", "rate_limited", "outcome_unknown"])(
    "leaves a %s refusal to its own message, promising no sign-in fix",
    (code) => {
      expect(
        commentWritesRecovery({
          block: null,
          refusal: { code, host: "github.com" },
          signInAvailable: true,
        }),
      ).toBeNull();
    },
  );
});
