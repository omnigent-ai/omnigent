import AzureMono from "@lobehub/icons/es/Azure/components/Mono";
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

  it("serves Azure DevOps copy", () => {
    const copy = gitProviderCopy("azure_devops");
    expect(copy).toBe(GIT_PROVIDERS.azure_devops);
    expect(copy).toMatchObject({
      id: "azure_devops",
      label: "Azure DevOps",
      Icon: AzureMono,
      prNumberPrefix: "!",
      prUrlPlaceholder: "https://dev.azure.com/org/project/_git/repo/pullrequest/123",
      defaultHost: "dev.azure.com",
      authHint: "Run `az login` on the host, or set `AZURE_DEVOPS_EXT_PAT`.",
      cliLabel: "Azure CLI",
    });
    expect(copy.repoUnresolvedHint).toBeUndefined();
  });

  it("lets only Azure DevOps sign in without its CLI", () => {
    expect(gitProviderCopy("azure_devops").signInWithoutCli).toBe(true);
    expect(gitProviderCopy("github").signInWithoutCli).toBeUndefined();
    expect(gitProviderCopy("nope").signInWithoutCli).toBeUndefined();
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
    expect(gitProviderCopy("azure_devops", "Run `nope login` on the host.").authHint).toBe(
      "Run `az login` on the host, or set `AZURE_DEVOPS_EXT_PAT`.",
    );
  });

  it("does not resolve inherited object keys as providers", () => {
    expect(gitProviderCopy("toString").label).toBe("toString");
  });
});
