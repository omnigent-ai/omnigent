import { describe, expect, it } from "vitest";
import type { PullRequestInfo } from "@/hooks/usePullRequests";
import {
  PULL_REQUEST_REF,
  gitHubPullRequestUrl,
  parsePullRequestRefHref,
  pullRequestRefUrl,
  remarkPullRequestRefs,
  sessionPullRequestRepo,
  splitPullRequestRefs,
} from "./pullRequestRefs";

function info(overrides: Partial<PullRequestInfo> = {}): PullRequestInfo {
  return {
    object: "session.github.info",
    available: true,
    repo: { name_with_owner: "acme/app" },
    ...overrides,
  };
}

/** The parked hrefs `text` produces, in order. */
function refHrefs(text: string): string[] {
  return splitPullRequestRefs(text).flatMap((node) =>
    node.type === "link" && node.url ? [node.url] : [],
  );
}

describe("splitPullRequestRefs", () => {
  it("parks bare and explicit references, keeping the surrounding text", () => {
    expect(splitPullRequestRefs("Fixed in #4242, see acme/app#12.")).toEqual([
      { type: "text", value: "Fixed in " },
      {
        type: "link",
        url: "#omnigent-pr-4242",
        title: null,
        children: [{ type: "text", value: "#4242" }],
      },
      { type: "text", value: ", see " },
      {
        type: "link",
        url: "#omnigent-pr-acme/app/12",
        title: null,
        children: [{ type: "text", value: "acme/app#12" }],
      },
      { type: "text", value: "." },
    ]);
  });

  it.each([
    ["(#1234)", ["#omnigent-pr-1234"]],
    ["#1234, #5678", ["#omnigent-pr-1234", "#omnigent-pr-5678"]],
    ["PR #1234: done", ["#omnigent-pr-1234"]],
    ["omnigent-ai/omnigent#9882", ["#omnigent-pr-omnigent-ai/omnigent/9882"]],
    ["socketio/socket.io#7", ["#omnigent-pr-socketio/socket.io/7"]],
    // Accepted: an all-digit hex color reads the same as a PR number.
    ["#123456", ["#omnigent-pr-123456"]],
  ])("links %j", (text, hrefs) => {
    expect(refHrefs(text)).toEqual(hrefs);
  });

  it.each([
    "#123",
    "#0123",
    "#fff",
    "#123abc",
    "#1234abc",
    "a#1234",
    "&#1234;",
    "file.md#L1234",
    "page/#1234",
    "##1234",
    "github.com/acme/app#12",
    "acme/app#012",
    "-acme/app#12",
    "acme/..#12",
    "#12345678901",
    "docs/setup.md#3",
    "src/foo.ts#12",
  ])("leaves %j as text", (text) => {
    expect(splitPullRequestRefs(text)).toEqual([{ type: "text", value: text }]);
  });

  it("keeps the boundary check free of lookbehind, which Safari < 16.4 can't parse", () => {
    expect(PULL_REQUEST_REF.source).not.toMatch(/\(\?<[=!]/);
  });

  it("splits a reference preceded by its boundary character", () => {
    expect(splitPullRequestRefs("x (#1234)")).toEqual([
      { type: "text", value: "x (" },
      {
        type: "link",
        url: "#omnigent-pr-1234",
        title: null,
        children: [{ type: "text", value: "#1234" }],
      },
      { type: "text", value: ")" },
    ]);
  });
});

describe("remarkPullRequestRefs", () => {
  it("skips code, raw HTML, and text that is already a link", () => {
    const opaque = [
      { type: "inlineCode", value: "#1234" },
      { type: "code", value: "#1234" },
      { type: "html", value: "<b>#1234</b>" },
      { type: "link", url: "https://x.test", children: [{ type: "text", value: "#1234" }] },
      { type: "linkReference", children: [{ type: "text", value: "#1234" }] },
    ];
    const tree = {
      type: "root",
      children: [{ type: "paragraph", children: structuredClone(opaque) }],
    };
    remarkPullRequestRefs()(tree);
    expect(tree.children[0].children).toEqual(opaque);
  });

  it("links text nested in emphasis and headings", () => {
    const tree = {
      type: "root",
      children: [
        {
          type: "heading",
          children: [{ type: "emphasis", children: [{ type: "text", value: "#4321" }] }],
        },
      ],
    };
    remarkPullRequestRefs()(tree);
    expect(tree.children[0].children[0].children).toMatchObject([
      { type: "link", url: "#omnigent-pr-4321" },
    ]);
  });
});

describe("parsePullRequestRefHref", () => {
  it("reads back what the plugin parks and rejects anything else", () => {
    expect(parsePullRequestRefHref("#omnigent-pr-1234")).toEqual({ repo: null, number: "1234" });
    expect(parsePullRequestRefHref("#omnigent-pr-acme/app/12")).toEqual({
      repo: "acme/app",
      number: "12",
    });
    for (const href of [
      undefined,
      "#omnigent-file",
      "#omnigent-pr-12",
      "#omnigent-pr-a/b/c/12",
      "#omnigent-pr-a/../12",
      "https://github.com/acme/app/pull/12",
    ]) {
      expect(parsePullRequestRefHref(href)).toBeNull();
    }
  });
});

describe("gitHubPullRequestUrl", () => {
  it("builds a github.com pull URL only from well-formed parts", () => {
    expect(gitHubPullRequestUrl("acme/app", "12")).toBe("https://github.com/acme/app/pull/12");
    const malformed = ["a/b?x", "../x", "a/..", "a/b/c", "javascript:alert(1)", "a/b#c", "a b/c"];
    for (const repo of malformed) {
      expect(gitHubPullRequestUrl(repo, "12")).toBeNull();
    }
    expect(gitHubPullRequestUrl("acme/app", "0")).toBeNull();
  });
});

describe("sessionPullRequestRepo", () => {
  it("uses the session's repo on GitHub, including hosts that predate providers", () => {
    expect(sessionPullRequestRepo(info({ provider: "github" }), null)).toBe("acme/app");
    expect(sessionPullRequestRepo(info(), null)).toBe("acme/app");
  });

  it("falls back only when the repo is unknown", () => {
    expect(sessionPullRequestRepo(undefined, "org/main")).toBe("org/main");
    expect(sessionPullRequestRepo(info({ repo: null, provider: "github" }), "org/main")).toBe(
      "org/main",
    );
    expect(sessionPullRequestRepo(info({ repo: null }), null)).toBeNull();
    expect(sessionPullRequestRepo(info({ repo: { name_with_owner: "a/b?x" } }), "org/main")).toBe(
      null,
    );
  });

  it("links nothing for other providers or GitHub Enterprise", () => {
    expect(sessionPullRequestRepo(info({ provider: "gitlab" }), "org/main")).toBeNull();
    expect(sessionPullRequestRepo(info({ remote_host: "ghe.example.com" }), "org/main")).toBeNull();
    const ghePr = { url: "https://ghe.example.com/acme/app/pull/3" } as PullRequestInfo["pr"];
    expect(sessionPullRequestRepo(info({ pr: ghePr }), "org/main")).toBeNull();
  });

  it("links nothing when no provider serves the workspace", () => {
    // The host's unsupported-remote payload can carry an empty `remote_host`.
    const unsupported = info({
      available: false,
      repo: null,
      reason: "unsupported_remote",
      remote_host: "",
      provider: null,
    });
    expect(sessionPullRequestRepo(unsupported, "org/main")).toBeNull();
    expect(sessionPullRequestRepo(info({ repo: null, provider: null }), "org/main")).toBeNull();
    expect(
      sessionPullRequestRepo(info({ repo: null, reason: "unsupported_remote" }), "org/main"),
    ).toBeNull();
    const ref = { repo: "other/lib", number: "5" };
    expect(pullRequestRefUrl(ref, unsupported, null)).toBeNull();
  });

  it("links without a PR only when the session's account is on github.com", () => {
    const signedIn = (host: string) =>
      info({
        provider: "github",
        auth: {
          authenticated: true,
          hint: null,
          cli: null,
          accounts: [{ login: "me", active: true, state: "success", host }],
          selected_account: "me",
        },
      });
    expect(sessionPullRequestRepo(signedIn("ghe.example.com"), "org/main")).toBeNull();
    expect(sessionPullRequestRepo(signedIn("github.com"), "org/main")).toBe("acme/app");
  });
});

describe("pullRequestRefUrl", () => {
  it("links an explicit repo regardless of the session's, but not off GitHub", () => {
    const ref = { repo: "other/lib", number: "5" };
    expect(pullRequestRefUrl(ref, info(), null)).toBe("https://github.com/other/lib/pull/5");
    expect(pullRequestRefUrl(ref, undefined, null)).toBe("https://github.com/other/lib/pull/5");
    expect(pullRequestRefUrl(ref, info({ provider: "azure_devops" }), null)).toBeNull();
  });
});
