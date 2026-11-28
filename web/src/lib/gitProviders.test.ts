import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { GitPullRequestIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { GIT_PROVIDERS, gitProviderCopy, hostedOnGitHub } from "@/lib/gitProviders";
import {
  type NamedCodeHost,
  type PullRequestIntegrations,
  pullRequestIntegrations,
  registerPullRequestIntegrations,
  restorePullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";

// A named host whose pull requests GitHub hosts, under its own name.
const EXAMPLE_NAMED_HOST: NamedCodeHost = {
  provider: "named-host",
  matchesHost: (host) => host.endsWith(".host.example.com"),
  copy: { ...GIT_PROVIDERS.github, id: "named-host", label: "Example Host" },
  hostedOnGitHub: true,
  readsAsDevice: true,
  readOnly: true,
  linksOnDevice: true,
};

describe("gitProviderCopy", () => {
  it("serves GitHub to a host that predates the provider field", () => {
    const copy = gitProviderCopy(undefined);
    expect(copy).toBe(GIT_PROVIDERS.github);
    expect(copy).toMatchObject({
      id: "github",
      label: "GitHub",
      Icon: GithubMono,
      prNumberPrefix: "#",
      prUrlPlaceholder: "https://github.com/owner/repo/pull/123",
      defaultHost: "github.com",
      authHint: "Run gh auth login on the host.",
      cliLabel: "GitHub CLI",
    });
    expect(gitProviderCopy("github")).toBe(copy);
  });

  it("uses an external provider's presentation without a frontend registration", () => {
    const copy = gitProviderCopy("example_forge", "Run `forge login`.", {
      id: "example_forge",
      display_name: "Example Forge",
      request_name: "merge request",
      number_prefix: "!",
    });
    expect(copy).toMatchObject({
      id: "example_forge",
      label: "Example Forge",
      requestName: "merge request",
      Icon: GitPullRequestIcon,
      prNumberPrefix: "!",
      prUrlPlaceholder: "Merge request URL",
      authHint: "Run `forge login`.",
      cliLabel: "Example Forge CLI",
    });
  });

  it("does not use another tracked provider's metadata", () => {
    expect(
      gitProviderCopy("other", undefined, {
        id: "example_forge",
        display_name: "Example Forge",
        request_name: "merge request",
        number_prefix: "!",
      }).prNumberPrefix,
    ).toBe("#");
  });

  it("builds neutral copy that names no provider when none is known", () => {
    const copy = gitProviderCopy(null);
    expect(copy).toMatchObject({
      label: "Pull Requests",
      Icon: GitPullRequestIcon,
      prNumberPrefix: "#",
      defaultHost: null,
    });
    expect(copy).not.toBe(GIT_PROVIDERS.github);
    const words = [copy.label, copy.prUrlPlaceholder, copy.authHint, copy.cliLabel].join(" ");
    expect(words).not.toMatch(/github/i);
  });

  it("builds generic copy for an unknown provider id", () => {
    expect(gitProviderCopy("nope")).toMatchObject({
      id: "nope",
      label: "nope",
      Icon: GitPullRequestIcon,
      prNumberPrefix: "#",
      defaultHost: null,
      authHint: "Sign in to nope on the host.",
      cliLabel: "nope CLI",
    });
    expect(gitProviderCopy("nope").repoUnresolvedHint).toBeUndefined();
  });

  it("takes the host's sign-in hint for an unknown provider only", () => {
    expect(gitProviderCopy("nope", "Run `nope login` on the host.").authHint).toBe(
      "Run `nope login` on the host.",
    );
    expect(gitProviderCopy("github", "Run `nope login` on the host.").authHint).toBe(
      "Run gh auth login on the host.",
    );
  });

  it("does not resolve inherited object keys as providers", () => {
    expect(gitProviderCopy("toString").label).toBe("toString");
  });
});

describe("gitProviderCopy for a registered named host", () => {
  let saved: PullRequestIntegrations;
  beforeEach(() => {
    saved = pullRequestIntegrations();
  });
  afterEach(() => restorePullRequestIntegrations(saved));

  it("serves the host's own copy and says whether GitHub hosts its pull requests", () => {
    expect(hostedOnGitHub("named-host")).toBe(false);
    registerPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_NAMED_HOST] });
    expect(gitProviderCopy("named-host")).toBe(EXAMPLE_NAMED_HOST.copy);
    expect(hostedOnGitHub("named-host")).toBe(true);
    expect(hostedOnGitHub("github")).toBe(true);
    expect(hostedOnGitHub(undefined)).toBe(true);
  });

  it("finds a host by its provider id, even when its copy names another", () => {
    const host = { ...EXAMPLE_NAMED_HOST, copy: { ...EXAMPLE_NAMED_HOST.copy, id: "shown-id" } };
    registerPullRequestIntegrations({ namedCodeHosts: [host] });
    expect(gitProviderCopy("named-host")).toBe(host.copy);
    expect(hostedOnGitHub("named-host")).toBe(true);
    expect(hostedOnGitHub("shown-id")).toBe(false);
  });

  it("says GitHub doesn't host a registered host's pull requests when it says so", () => {
    registerPullRequestIntegrations({
      namedCodeHosts: [{ ...EXAMPLE_NAMED_HOST, hostedOnGitHub: false }],
    });
    expect(gitProviderCopy("named-host")).toBe(EXAMPLE_NAMED_HOST.copy);
    expect(hostedOnGitHub("named-host")).toBe(false);
  });
});

describe("hostedOnGitHub", () => {
  it("agrees with gitProviderCopy on which ids are GitHub", () => {
    for (const id of [undefined, "", "github"]) {
      expect(gitProviderCopy(id)).toBe(GIT_PROVIDERS.github);
      expect(hostedOnGitHub(id)).toBe(true);
    }
    expect(gitProviderCopy(null)).not.toBe(GIT_PROVIDERS.github);
    expect(hostedOnGitHub(null)).toBe(false);
    expect(hostedOnGitHub("unknown-host")).toBe(false);
  });
});
