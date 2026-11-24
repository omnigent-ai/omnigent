import { describe, expect, it } from "vitest";

import {
  codeHostProvider,
  parseCodeHostPullRequestUrl,
  pullRequestLink,
  type PullRequestLinks,
} from "./codeHosts";

const PULL_REQUEST = "https://github.com/example-org/app/pull/1234";

describe("codeHostProvider", () => {
  it("names github.com from the host alone, in any case", () => {
    expect(codeHostProvider("github.com")).toBe("github");
    expect(codeHostProvider("GitHub.com")).toBe("github");
  });

  it("treats an Enterprise host as GitHub only with evidence", () => {
    expect(codeHostProvider("ghe.example.com")).toBeNull();
    expect(codeHostProvider("ghe.example.com", ["GHE.example.com"])).toBe("github");
  });

  it("doesn't recognize a host that only looks like github.com", () => {
    expect(codeHostProvider("github.com.evil.example")).toBeNull();
    expect(codeHostProvider("evil-github.com")).toBeNull();
  });
});

describe("parseCodeHostPullRequestUrl", () => {
  it("reads a github.com pull request", () => {
    expect(parseCodeHostPullRequestUrl(PULL_REQUEST)).toEqual({
      provider: "github",
      host: "github.com",
      repository: "example-org/app",
      number: 1234,
      url: PULL_REQUEST,
    });
  });

  it("drops a query, fragment, and trailing slash", () => {
    expect(parseCodeHostPullRequestUrl(`${PULL_REQUEST}/?w=1#top`)?.url).toBe(PULL_REQUEST);
  });

  it("reads an Enterprise host only with evidence", () => {
    expect(parseCodeHostPullRequestUrl("https://ghe.example.com/o/r/pull/7")).toBeNull();
    expect(
      parseCodeHostPullRequestUrl("https://ghe.example.com/o/r/pull/7", ["ghe.example.com"]),
    ).toMatchObject({ provider: "github", host: "ghe.example.com", repository: "o/r", number: 7 });
  });

  it.each([
    ["nothing", null],
    ["not a link", "example-org/app#1234"],
    ["plain http", "http://github.com/o/r/pull/7"],
    ["a port", "https://github.com:8443/o/r/pull/7"],
    ["credentials", "https://user:secret@github.com/o/r/pull/7"],
    ["an issue", "https://github.com/o/r/issues/7"],
    ["a pull request's files tab", "https://github.com/o/r/pull/7/files"],
    ["no number", "https://github.com/o/r/pull/x"],
  ])("reads nothing from %s", (_case, url) => {
    expect(parseCodeHostPullRequestUrl(url)).toBeNull();
  });
});

describe("pullRequestLink", () => {
  const REVIEW: PullRequestLinks = {
    url_template: "https://review.example.com/{host}/{owner}/{repo}/pull/{number}",
    name: "Code Review",
    applies_to: ["github.com/example-org", "ghe.example.com", "github.com/other-org/tools"],
  };

  it.each([
    [
      "a github.com pull request under an owner prefix",
      PULL_REQUEST,
      "https://review.example.com/github.com/example-org/app/pull/1234",
    ],
    [
      "an Enterprise pull request under a host prefix",
      "https://ghe.example.com/team/svc/pull/7/",
      "https://review.example.com/ghe.example.com/team/svc/pull/7",
    ],
    [
      "an owner whose case differs from the prefix",
      "https://github.com/Example-Org/app/pull/1234",
      "https://review.example.com/github.com/Example-Org/app/pull/1234",
    ],
    [
      "a repository under a repository prefix",
      "https://github.com/other-org/tools/pull/5",
      "https://review.example.com/github.com/other-org/tools/pull/5",
    ],
    [
      "a repository spelled with escaped characters, decoded once",
      "https://github.com/example-org/ap%70/pull/42",
      "https://review.example.com/github.com/example-org/app/pull/42",
    ],
  ])("moves %s to the destination", (_case, url, href) => {
    expect(pullRequestLink(url, REVIEW)).toEqual({ href, destination: "Code Review" });
  });

  it.each([
    ["another owner on the same host", "https://github.com/other-org/app/pull/1"],
    ["an owner that only starts with the prefix", "https://github.com/example-org-fork/app/pull/1"],
    ["an unlisted Enterprise host", "https://ghe2.example.com/team/svc/pull/7"],
    ["a sibling of a listed repository", "https://github.com/other-org/docs/pull/5"],
    [
      "a repository that only starts with a listed one",
      "https://github.com/other-org/tools-v2/pull/5",
    ],
    ["an escaped slash in the owner", "https://github.com/example-org%2Fx/app/pull/5"],
    ["an escaped space in the repository", "https://github.com/example-org/a%20b/pull/5"],
    ["a malformed escape", "https://github.com/example-org/a%zz/pull/5"],
    ["a pull request's files tab", `${PULL_REQUEST}/files`],
    ["a comment on the pull request", `${PULL_REQUEST}#issuecomment-1`],
    ["a pull request with a query", `${PULL_REQUEST}?w=1`],
    ["an issue", "https://github.com/example-org/app/issues/7"],
    ["a merge request", "https://gitlab.example.com/example-org/app/-/merge_requests/7"],
    ["a relative link", "/c/session-id"],
  ])("leaves %s on its code host", (_case, url) => {
    expect(pullRequestLink(url, REVIEW)).toEqual({ href: url, destination: null });
  });

  it("leaves every link alone when the deployment sets no destination", () => {
    expect(pullRequestLink(PULL_REQUEST, null)).toEqual({ href: PULL_REQUEST, destination: null });
  });
});
