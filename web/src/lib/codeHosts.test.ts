import { describe, expect, it } from "vitest";

import { codeHostProvider, parseCodeHostPullRequestUrl } from "./codeHosts";

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
