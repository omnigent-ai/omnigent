import { describe, expect, it } from "vitest";

import { pullRequestIdentityLabels, type NamedPullRequest } from "@/lib/pullRequestIdentity";

const OTHER_PROVIDER_DISPLAY = {
  id: "other-git",
  display_name: "Other Git",
  request_name: "pull request",
  number_prefix: "#",
};

function pr(host: string, repository: string, number: number, provider?: string): NamedPullRequest {
  return {
    url: `https://${host || "unknown"}/${repository}/pull/${number}#${provider ?? "github"}`,
    host,
    repository,
    number,
    provider,
    provider_display: provider === "other-git" ? OTHER_PROVIDER_DISPLAY : undefined,
  };
}

const labels = (prs: NamedPullRequest[]) => [...pullRequestIdentityLabels(prs).values()];

describe("pullRequestIdentityLabels", () => {
  it("names GitHub PRs by repository and number, and other providers by name", () => {
    expect(
      labels([
        pr("github.com", "acme/app", 1),
        pr("git2.example.com", "example-org/widgets", 7, "other-git"),
      ]),
    ).toEqual(["acme/app #1", "Other Git · example-org/widgets #7"]);
  });

  it("adds the host only when another listed PR would read the same", () => {
    expect(
      labels([
        pr("git1.example.com", "org/repo", 7, "other-git"),
        pr("git2.example.com", "org/repo", 7, "other-git"),
        pr("git2.example.com", "org/repo", 8, "other-git"),
      ]),
    ).toEqual([
      "Other Git · git1.example.com · org/repo #7",
      "Other Git · git2.example.com · org/repo #7",
      "Other Git · org/repo #8",
    ]);
    expect(labels([pr("github.com", "acme/app", 3), pr("GHE.example.com", "Acme/App", 3)])).toEqual(
      ["github.com · acme/app #3", "ghe.example.com · Acme/App #3"],
    );
    // The same number on another provider doesn't read alike.
    expect(
      labels([pr("github.com", "org/repo", 7), pr("git1.example.com", "org/repo", 7, "other-git")]),
    ).toEqual(["org/repo #7", "Other Git · org/repo #7"]);
  });

  it("says when a PR has no known host", () => {
    expect(labels([pr("", "acme/app", 4)])).toEqual(["acme/app #4 (unknown host)"]);
  });
});
