import { describe, expect, it } from "vitest";

import type { PullRequest, PullRequestAssociation } from "@/hooks/usePullRequests";

import {
  recentPullRequestRows,
  recentPullRequestUrl,
  type RecentPullRequestFacts,
  type RecentPullRequestRow,
} from "./recentPullRequestRow";

const URL_338 = "https://github.com/acme/widgets/pull/338";
const ENTERPRISE_338 = "https://ghe.example.com/acme/widgets/pull/338";

function linked(
  url: string,
  overrides: Partial<PullRequestAssociation> = {},
): PullRequestAssociation {
  return {
    url,
    host: new URL(url).hostname,
    repository: "acme/widgets",
    number: 338,
    title: "Show who wrote each review comment",
    relationship: "created",
    ...overrides,
  };
}

function details(url: string, overrides: Partial<PullRequest> = {}): PullRequest {
  return {
    url,
    number: 338,
    title: "Linked title",
    author: "jane-doe",
    ...overrides,
  } as PullRequest;
}

function facts(
  linkedPrs: PullRequestAssociation[] = [],
  detailsByUrl: [string, PullRequest][] = [],
): RecentPullRequestFacts {
  return { linked: linkedPrs, details: new Map(detailsByUrl) };
}

function row(url: string, known: RecentPullRequestFacts, urls = [url]): RecentPullRequestRow {
  return recentPullRequestRows(urls, known).get(url)!;
}

describe("recentPullRequestRows", () => {
  it("leads with the title, then the author and repository, with the number at the trailing edge", () => {
    expect(
      row(
        URL_338,
        facts(
          [linked(URL_338)],
          [[URL_338, details(URL_338, { title: "Show who wrote each review comment" })]],
        ),
      ),
    ).toEqual({
      label: "Show who wrote each review comment",
      description: "jane-doe · acme/widgets",
      detail: "#338",
      keywords: ["pull request", "jane-doe", "acme/widgets", "#338", "338"],
    });
  });

  it("prefers the PR's own details over the linked list's title", () => {
    const known = facts(
      [linked(URL_338)],
      [[URL_338, details(URL_338, { title: "Renamed on the host" })]],
    );
    expect(row(URL_338, known).label).toBe("Renamed on the host");
  });

  it("leaves the author out until the PR's details have been read", () => {
    expect(row(URL_338, facts([linked(URL_338)]))).toMatchObject({
      label: "Show who wrote each review comment",
      description: "acme/widgets",
      detail: "#338",
    });
  });

  it("falls back to the number, read from the URL, when nothing names the PR", () => {
    expect(row(URL_338, facts())).toEqual({
      label: "Pull request #338",
      description: "acme/widgets",
      keywords: ["pull request", "acme/widgets", "#338", "338"],
    });
  });

  it("names a PR the session no longer links from its cached details", () => {
    expect(row(URL_338, facts([], [[URL_338, details(URL_338)]]))).toMatchObject({
      label: "Linked title",
      description: "jane-doe · acme/widgets",
      detail: "#338",
    });
  });

  it("tells apart two linked PRs that read alike on github.com and an Enterprise host", () => {
    const known = facts([linked(URL_338), linked(ENTERPRISE_338)]);
    const rows = recentPullRequestRows([URL_338, ENTERPRISE_338], known);
    expect(rows.get(URL_338)?.description).toBe("github.com · acme/widgets");
    expect(rows.get(ENTERPRISE_338)?.description).toBe("ghe.example.com · acme/widgets");
    expect(rows.get(ENTERPRISE_338)?.keywords).toContain("ghe.example.com");
  });

  it("tells apart two hosts of one provider with the same repository and number", () => {
    const one = "https://git1.example.com/acme/widgets/pull/338";
    const two = "https://git2.example.com/acme/widgets/pull/338";
    const display = { id: "acme_forge", display_name: "Acme Forge", number_prefix: "#" } as never;
    const known = facts([
      linked(one, { provider: "acme_forge", provider_display: display }),
      linked(two, { provider: "acme_forge", provider_display: display }),
    ]);
    const rows = recentPullRequestRows([one, two], known);
    expect(rows.get(one)?.description).toBe("Acme Forge · git1.example.com · acme/widgets");
    expect(rows.get(two)?.description).toBe("Acme Forge · git2.example.com · acme/widgets");
    expect(rows.get(two)?.keywords).toEqual(
      expect.arrayContaining(["Acme Forge", "git2.example.com"]),
    );
  });

  it("tells a recent PR the session no longer links apart from a linked lookalike", () => {
    const rows = recentPullRequestRows([URL_338, ENTERPRISE_338], facts([linked(URL_338)]));
    expect(rows.get(URL_338)?.description).toBe("github.com · acme/widgets");
    expect(rows.get(ENTERPRISE_338)?.description).toBe("ghe.example.com · acme/widgets");
  });

  it("names a provider other than GitHub, with its number prefix", () => {
    const url = "https://forge.example.com/acme/widgets/-/merge_requests/12";
    const known = facts([
      linked(url, {
        number: 12,
        title: "Cache the build",
        provider: "acme_forge",
        provider_display: {
          id: "acme_forge",
          display_name: "Acme Forge",
          number_prefix: "!",
        } as never,
      }),
    ]);
    expect(row(url, known)).toMatchObject({
      label: "Cache the build",
      description: "Acme Forge · acme/widgets",
      detail: "!12",
    });
    expect(row(url, known).keywords).toEqual(expect.arrayContaining(["Acme Forge", "!12", "12"]));
  });

  it("ignores a blank title", () => {
    expect(row(URL_338, facts([linked(URL_338, { title: "  " })])).label).toBe("Pull request #338");
  });
});

describe("recentPullRequestUrl", () => {
  it("is the PR itself, not one of its files", () => {
    expect(recentPullRequestUrl({ kind: "pull", url: URL_338, view: "summary" })).toBe(URL_338);
    expect(
      recentPullRequestUrl({ kind: "pull", url: URL_338, view: "changes", path: "a.ts" }),
    ).toBeUndefined();
    expect(recentPullRequestUrl({ kind: "pulls" })).toBeUndefined();
  });
});
