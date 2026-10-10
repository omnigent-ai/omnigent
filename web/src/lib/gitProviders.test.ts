import GithubMono from "@lobehub/icons/es/Github/components/Mono";
import { GitPullRequestIcon } from "lucide-react";
import { describe, expect, it } from "vitest";
import { GIT_PROVIDERS, gitProviderCopy } from "@/lib/gitProviders";

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
