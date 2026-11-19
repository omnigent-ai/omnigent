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
    repo: { name_with_owner: "acme/app", host: "github.com" },
    workspace_repo: { name_with_owner: "acme/app", host: "github.com" },
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
    expect(splitPullRequestRefs("Fixed in #4242, see acme/app#12 (#1234).")).toEqual([
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
      // The boundary character before a reference stays in the text.
      { type: "text", value: " (" },
      {
        type: "link",
        url: "#omnigent-pr-1234",
        title: null,
        children: [{ type: "text", value: "#1234" }],
      },
      { type: "text", value: ")." },
    ]);
  });

  it.each([
    ["(#1234)", ["#omnigent-pr-1234"]],
    ["#1234, #5678", ["#omnigent-pr-1234", "#omnigent-pr-5678"]],
    ["PR #1234: done", ["#omnigent-pr-1234"]],
    ["omnigent-ai/omnigent#9882", ["#omnigent-pr-omnigent-ai/omnigent/9882"]],
    ["octocat/hello-world#5", ["#omnigent-pr-octocat/hello-world/5"]],
    ["octo-org/.github#12", ["#omnigent-pr-octo-org/.github/12"]],
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
    "styles/main.css#1234",
    "docs/index.html#1234",
    "config/app.toml#1234",
    "src/app.js#12",
    "src/index.mjs#1234",
    "infra/main.tf#123",
    "api/x.proto#12",
    "vercel/next.js#123",
    "socketio/socket.io#7",
    "assets/logo.png#1234",
    "docs/report.pdf#1234",
    "app/build.gradle#12",
    "src/App.svelte#3",
    "and/or#1234",
    "origin/main#1",
    "upstream/main#12",
    "24/7#1234",
    "src/schema.graphql#1234",
    "cache/build.tsbuildinfo#1234",
  ])("leaves %j as text", (text) => {
    expect(splitPullRequestRefs(text)).toEqual([{ type: "text", value: text }]);
  });

  it("keeps the boundary check free of lookbehind, which Safari < 16.4 can't parse", () => {
    expect(PULL_REQUEST_REF.source).not.toMatch(/\(\?<[=!]/);
  });
});

describe("remarkPullRequestRefs", () => {
  it("skips code, raw HTML, and text that is already a link", () => {
    const opaque = [
      { type: "inlineCode", value: "#1234" },
      { type: "code", value: "#1234" },
      { type: "html", value: "<b>#1234</b>" },
      { type: "html", value: "<code>" },
      { type: "text", value: "#1234" },
      { type: "html", value: "</code>" },
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

  it("skips text inside an inline raw HTML anchor, whose tags parse as siblings", () => {
    const anchored = [
      { type: "text", value: "See " },
      { type: "html", value: '<A href="https://example.com/raw">' },
      { type: "text", value: "#2468 " },
      { type: "emphasis", children: [{ type: "text", value: "#1357" }] },
      { type: "html", value: "</a>" },
    ];
    const tree = {
      type: "root",
      children: [
        {
          type: "paragraph",
          children: [...structuredClone(anchored), { type: "text", value: " then #4321" }],
        },
      ],
    };
    remarkPullRequestRefs()(tree);
    expect(tree.children[0].children).toEqual([
      ...anchored,
      { type: "text", value: " then " },
      {
        type: "link",
        url: "#omnigent-pr-4321",
        title: null,
        children: [{ type: "text", value: "#4321" }],
      },
    ]);
  });

  it.each([
    ["<a>", "</a>"],
    ["<a/>", "</a>"],
    ['<a\nhref="https://example.com/raw">', "</a>"],
    ["<code>", "</code>"],
    ["<PRE>", "</pre>"],
    ["<kbd>", "</kbd>"],
    ['<samp class="x"/>', "</SAMP >"],
  ])("skips text between raw %j and %j tags", (open, close) => {
    const anchored = [
      { type: "html", value: open },
      { type: "text", value: "#2468" },
      { type: "html", value: close },
    ];
    const tree = {
      type: "root",
      children: [{ type: "paragraph", children: structuredClone(anchored) }],
    };
    remarkPullRequestRefs()(tree);
    expect(tree.children[0].children).toEqual(anchored);
  });

  it("keeps linking after a stray raw closing tag", () => {
    const tree = {
      type: "root",
      children: [
        {
          type: "paragraph",
          children: [
            { type: "html", value: "</code>" },
            { type: "text", value: "#2468" },
          ],
        },
      ],
    };
    remarkPullRequestRefs()(tree);
    expect(tree.children[0].children[1]).toMatchObject({ type: "link", url: "#omnigent-pr-2468" });
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
  it("resolves bare references against the workspace's repo, not the tracked PR's", () => {
    const tracked = info({
      repo: { name_with_owner: "owner/other", host: "github.com" },
      selected_pr_url: "https://github.com/owner/other/pull/3",
      workspace_repo: { name_with_owner: "owner/app", host: "github.com" },
    });
    expect(sessionPullRequestRepo(tracked, null)).toBe("owner/app");
    expect(pullRequestRefUrl({ repo: null, number: "1234" }, tracked)).toBe(
      "https://github.com/owner/app/pull/1234",
    );
  });

  it("links nothing in an Enterprise workspace, even one tracking a github.com PR", () => {
    const tracked = info({
      provider: "github",
      repo: { name_with_owner: "owner/other", host: "github.com" },
      pr: { url: "https://github.com/owner/other/pull/3" } as PullRequestInfo["pr"],
      selected_pr_url: "https://github.com/owner/other/pull/3",
      workspace_repo: { name_with_owner: "owner/ws", host: "ghe.example.com" },
    });
    expect(sessionPullRequestRepo(tracked, "org/main")).toBeNull();
    expect(pullRequestRefUrl({ repo: null, number: "1234" }, tracked)).toBeNull();
    expect(pullRequestRefUrl({ repo: "octocat/hello-world", number: "5" }, tracked)).toBeNull();
  });

  it("leaves text when the workspace's repo isn't reported, as from an older server", () => {
    const older = info({ workspace_repo: undefined });
    expect(sessionPullRequestRepo(older, "org/main")).toBeNull();
    expect(pullRequestRefUrl({ repo: null, number: "4242" }, older)).toBeNull();
    expect(pullRequestRefUrl({ repo: "o/r", number: "1" }, older)).toBeNull();
    const hostless = info({ workspace_repo: { name_with_owner: "corp/app" } });
    expect(sessionPullRequestRepo(hostless, "org/main")).toBeNull();
    expect(sessionPullRequestRepo(undefined, "org/main")).toBeNull();
  });

  it("ignores signed-in accounts, which don't name the remote's host", () => {
    const signedIn = info({
      workspace_repo: { name_with_owner: "corp/app" },
      auth: {
        authenticated: true,
        hint: null,
        cli: null,
        accounts: [{ login: "me", active: true, state: "success", host: "github.com" }],
        selected_account: "me",
      },
    });
    expect(sessionPullRequestRepo(signedIn, "org/main")).toBeNull();
  });

  it("falls back only when the workspace's repo is unnamed", () => {
    const unnamed = { name_with_owner: null, host: "github.com" };
    expect(sessionPullRequestRepo(info({ workspace_repo: unnamed }), "org/main")).toBe("org/main");
    expect(sessionPullRequestRepo(info({ workspace_repo: unnamed }), null)).toBeNull();
    expect(
      sessionPullRequestRepo(
        info({ workspace_repo: { name_with_owner: "a/b?x", host: "github.com" } }),
        "org/main",
      ),
    ).toBeNull();
  });

  it("links nothing for a workspace on another forge", () => {
    const gitlab = info({
      provider: "gitlab",
      workspace_repo: { name_with_owner: "acme/app", host: "gitlab.com" },
    });
    expect(sessionPullRequestRepo(gitlab, "org/main")).toBeNull();
    expect(pullRequestRefUrl({ repo: "other/lib", number: "5" }, gitlab, null)).toBeNull();
  });
});

describe("pullRequestRefUrl", () => {
  it("links an explicit repo regardless of the workspace's, but only on github.com", () => {
    const ref = { repo: "other/lib", number: "5" };
    expect(pullRequestRefUrl(ref, info(), null)).toBe("https://github.com/other/lib/pull/5");
    expect(pullRequestRefUrl(ref, undefined, null)).toBeNull();
    const ghe = { name_with_owner: "acme/app", host: "ghe.example.com" };
    expect(pullRequestRefUrl(ref, info({ workspace_repo: ghe }), null)).toBeNull();
  });
});
