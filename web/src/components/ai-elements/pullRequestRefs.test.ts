import { describe, expect, it } from "vitest";
import type { PullRequestInfo } from "@/hooks/usePullRequests";
import {
  PULL_REQUEST_REF,
  PULL_REQUEST_REF_HREF_PREFIX,
  gitHubPullRequestUrl,
  linkedPullRequestUrl,
  parsePullRequestRefHref,
  pullRequestRefUrl,
  remarkPullRequestRefs,
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
        url: `${PULL_REQUEST_REF_HREF_PREFIX}4242`,
        title: null,
        children: [{ type: "text", value: "#4242" }],
      },
      { type: "text", value: ", see " },
      {
        type: "link",
        url: `${PULL_REQUEST_REF_HREF_PREFIX}acme/app/12`,
        title: null,
        children: [{ type: "text", value: "acme/app#12" }],
      },
      // The boundary character before a reference stays in the text.
      { type: "text", value: " (" },
      {
        type: "link",
        url: `${PULL_REQUEST_REF_HREF_PREFIX}1234`,
        title: null,
        children: [{ type: "text", value: "#1234" }],
      },
      { type: "text", value: ")." },
    ]);
  });

  it.each([
    ["(#1234)", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    [
      "#1234, #5678",
      [`${PULL_REQUEST_REF_HREF_PREFIX}1234`, `${PULL_REQUEST_REF_HREF_PREFIX}5678`],
    ],
    ["PR #1234: done", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    ["omnigent-ai/omnigent#9882", [`${PULL_REQUEST_REF_HREF_PREFIX}omnigent-ai/omnigent/9882`]],
    ["octocat/hello-world#5", [`${PULL_REQUEST_REF_HREF_PREFIX}octocat/hello-world/5`]],
    ["octo-org/.github#12", [`${PULL_REQUEST_REF_HREF_PREFIX}octo-org/.github/12`]],
    // Accepted: an all-digit hex color reads the same as a PR number.
    ["#123456", [`${PULL_REQUEST_REF_HREF_PREFIX}123456`]],
    ["Fixed in #1234", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    ["é #1234", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    // Scripts written without spaces between words border a reference directly.
    ["修复#4242", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    ["修复了#1234。", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    ["#1234を修正", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    // Japanese marks like the prolonged sound mark count with their scripts.
    ["#4242ーを修正", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    ["ーー#4242", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    [
      "修复#1234和#5678",
      [`${PULL_REQUEST_REF_HREF_PREFIX}1234`, `${PULL_REQUEST_REF_HREF_PREFIX}5678`],
    ],
    ["수정#1234", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`]],
    ["修复acme/app#12", [`${PULL_REQUEST_REF_HREF_PREFIX}acme/app/12`]],
    ["See #4242.", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    ["(#4242).", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    ["#4242.)", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    ["#1234,#5678", [`${PULL_REQUEST_REF_HREF_PREFIX}1234`, `${PULL_REQUEST_REF_HREF_PREFIX}5678`]],
    ["#4242, then", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    ["#4242.\nNext", [`${PULL_REQUEST_REF_HREF_PREFIX}4242`]],
    // Any bare number parks; it links only to a PR the session links.
    [
      "See #1 and #123.",
      [`${PULL_REQUEST_REF_HREF_PREFIX}1`, `${PULL_REQUEST_REF_HREF_PREFIX}123`],
    ],
  ])("links %j", (text, hrefs) => {
    expect(refHrefs(text)).toEqual(hrefs);
  });

  it.each([
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
    "styles/main.css#1234",
    "vercel/next.js#123",
    "app/build.gradle#12",
    "and/or#1234",
    "origin/main#1",
    "upstream/main#12",
    "24/7#1234",
    "feature/login#12",
    "docs/README#3",
    "acme-/app#12",
    "naïve/app#12",
    "résumé#1234",
    "docs/说明#1234",
    "docs/说明acme/app#12",
    "docs/𠀀明#1234",
    "docs/ーデ#1234",
    "docs\\𠀀明#1234",
    "#1234é",
    "#4242.md",
    "#1234.5",
    "Read #4242.json",
    "acme/app#12.txt",
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
        url: `${PULL_REQUEST_REF_HREF_PREFIX}4321`,
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

  it("keeps linking after a raw HTML block that closes its own tags", () => {
    const tree = {
      type: "root",
      children: [
        { type: "html", value: "<pre>\n<code>#1111</code>\n</pre>" },
        { type: "paragraph", children: [{ type: "text", value: "See #4321" }] },
      ],
    };
    remarkPullRequestRefs()(tree);
    expect(tree.children[1].children?.[1]).toMatchObject({
      type: "link",
      url: `${PULL_REQUEST_REF_HREF_PREFIX}4321`,
    });
  });

  it("ignores a raw closing tag that closes nothing open", () => {
    const opaque = [
      { type: "html", value: "<code>" },
      { type: "text", value: "#2468" },
      { type: "html", value: "</a>" },
      { type: "text", value: " #1357" },
      { type: "html", value: "</code>" },
    ];
    const tree = {
      type: "root",
      children: [
        {
          type: "paragraph",
          children: [
            { type: "html", value: "</code>" },
            { type: "text", value: "#1111 " },
            ...structuredClone(opaque),
            { type: "text", value: " #4321" },
          ],
        },
      ],
    };
    remarkPullRequestRefs()(tree);
    const children = tree.children[0].children;
    expect(children[1]).toMatchObject({ type: "link", url: `${PULL_REQUEST_REF_HREF_PREFIX}1111` });
    expect(children.slice(3, 3 + opaque.length)).toEqual(opaque);
    expect(children.at(-1)).toMatchObject({
      type: "link",
      url: `${PULL_REQUEST_REF_HREF_PREFIX}4321`,
    });
  });

  // Thousands of alternating escaped and literal refs in one node still map one to one.
  const longNumbers = Array.from({ length: 5000 }, (_, index) => String(1000 + index));
  const longRow: [string, string, string, string[]] = [
    "5000 alternating escaped and literal refs",
    longNumbers.map((number, index) => (index % 2 ? `#${number}` : `\\#${number}`)).join(" "),
    longNumbers.map((number) => `#${number}`).join(" "),
    longNumbers.filter((_, index) => index % 2),
  ];

  it.each([
    ["an escaped hash and reference", "a &#35;1234 \\#2345 #5678", "a #1234 #2345 #5678", ["5678"]],
    ["a numeric reference", "a &#1234; #5678", "a \u04d2 #5678", ["5678"]],
    // A source that doesn't account for every `#` links nothing.
    ["a misread source", "a b c", "a #1234", []],
    // Neither does a node the source can't place.
    ["a node without a position", null, "a #1234", []],
    longRow,
  ])("reads escaped hashes for %s from the source", (_label, raw, value, linked) => {
    const source = raw ?? "a #1234";
    const position =
      raw === null ? undefined : { start: { offset: 0 }, end: { offset: source.length } };
    const tree = {
      type: "root",
      children: [{ type: "paragraph", children: [{ type: "text", value, position }] }],
    };
    remarkPullRequestRefs()(tree, { value: source });
    const urls = tree.children[0].children.flatMap((node) => ("url" in node ? [node.url] : []));
    expect(urls).toEqual(linked.map((number) => `${PULL_REQUEST_REF_HREF_PREFIX}${number}`));
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
      { type: "link", url: `${PULL_REQUEST_REF_HREF_PREFIX}4321` },
    ]);
  });
});

describe("parsePullRequestRefHref", () => {
  it("reads back what the plugin parks and rejects anything else", () => {
    expect(parsePullRequestRefHref(`${PULL_REQUEST_REF_HREF_PREFIX}1234`)).toEqual({
      repo: null,
      number: "1234",
    });
    expect(parsePullRequestRefHref(`${PULL_REQUEST_REF_HREF_PREFIX}acme/app/12`)).toEqual({
      repo: "acme/app",
      number: "12",
    });
    expect(parsePullRequestRefHref(`${PULL_REQUEST_REF_HREF_PREFIX}12`)).toEqual({
      repo: null,
      number: "12",
    });
    for (const href of [
      undefined,
      "#omnigent-file",
      `${PULL_REQUEST_REF_HREF_PREFIX}012`,
      `${PULL_REQUEST_REF_HREF_PREFIX}a/b/c/12`,
      `${PULL_REQUEST_REF_HREF_PREFIX}a/../12`,
      "https://github.com/acme/app/pull/12",
      // A hand-written fragment can't forge a parked reference.
      "#omnigent-pr-1234",
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

/** A PR the session links, as its PR info lists it. */
function linked(url: string, relationship: "created" | "attached" | "inferred" = "attached") {
  const [, repository, number] = url.match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)(?:[/?#]|$)/)!;
  return { url, host: "github.com", repository, number: Number(number), relationship };
}

describe("linkedPullRequestUrl", () => {
  it("links a bare number to the session's linked PR with it, in whichever repo", () => {
    const session = info({
      prs: [
        linked("https://github.com/acme/app/pull/4242"),
        linked("https://github.com/other/lib/pull/14971", "created"),
      ],
    });
    expect(pullRequestRefUrl({ repo: null, number: "4242" }, session)).toBe(
      "https://github.com/acme/app/pull/4242",
    );
    // Not the workspace's repo: the link names where the PR actually is.
    expect(pullRequestRefUrl({ repo: null, number: "14971" }, session)).toBe(
      "https://github.com/other/lib/pull/14971",
    );
  });

  it("leaves a bare number the session doesn't link as text, whatever the workspace's repo", () => {
    const session = info({ prs: [linked("https://github.com/acme/app/pull/4242")] });
    expect(pullRequestRefUrl({ repo: null, number: "4243" }, session)).toBeNull();
    expect(pullRequestRefUrl({ repo: null, number: "4242" }, info())).toBeNull();
    expect(pullRequestRefUrl({ repo: null, number: "4242" }, undefined)).toBeNull();
  });

  it("leaves a number two linked repos share as text, since either could be meant", () => {
    const session = info({
      prs: [
        linked("https://github.com/acme/app/pull/12"),
        linked("https://github.com/other/lib/pull/12"),
      ],
    });
    expect(linkedPullRequestUrl(session, "12")).toBeNull();
    // The same PR listed twice, in another letter case, is still one PR.
    const twice = info({
      prs: [
        linked("https://github.com/Acme/App/pull/12"),
        linked("https://github.com/acme/app/pull/12", "inferred"),
      ],
    });
    expect(linkedPullRequestUrl(twice, "12")).toBe("https://github.com/Acme/App/pull/12");
  });

  it("counts one PR however its stored link ends: a trailing slash, a query or a fragment", () => {
    const session = info({
      prs: [
        linked("https://github.com/acme/app/pull/12"),
        linked("https://github.com/acme/app/pull/12/", "inferred"),
        linked("https://github.com/acme/app/pull/12?tab=files", "attached"),
        linked("https://github.com/acme/app/pull/12#discussion_r1", "created"),
      ],
    });
    expect(linkedPullRequestUrl(session, "12")).toBe("https://github.com/acme/app/pull/12");
  });

  it("links only providers whose #N names a pull request", () => {
    const gitlab = {
      url: "https://gitlab.example.com/acme/app/-/merge_requests/12",
      host: "gitlab.example.com",
      repository: "acme/app",
      number: 12,
      relationship: "attached" as const,
      provider: "gitlab",
      provider_display: {
        id: "gitlab",
        display_name: "GitLab",
        request_name: "merge request",
        number_prefix: "!",
      },
    };
    // On GitLab `#12` is an issue; the merge request is `!12`.
    expect(linkedPullRequestUrl(info({ prs: [gitlab] }), "12")).toBeNull();
    const azure = {
      ...gitlab,
      url: "https://dev.azure.example.com/acme/app/_git/app/pullrequest/12",
      provider: "azure_devops",
      provider_display: { ...gitlab.provider_display, id: "azure_devops" },
    };
    expect(linkedPullRequestUrl(info({ prs: [azure] }), "12")).toBeNull();
    // A provider without its display copy can't say what `#N` means there.
    const { provider_display: _display, ...bare } = gitlab;
    expect(linkedPullRequestUrl(info({ prs: [bare] }), "12")).toBeNull();
    const hashed = {
      ...gitlab,
      url: "https://code.example.com/acme/app/pulls/12",
      provider: "forge",
      provider_display: { ...gitlab.provider_display, id: "forge", number_prefix: "#" },
    };
    expect(linkedPullRequestUrl(info({ prs: [hashed] }), "12")).toBe(hashed.url);
    // A GitHub PR with the same number beside a GitLab one is the only `#12`.
    const github = linked("https://github.com/acme/app/pull/12");
    expect(linkedPullRequestUrl(info({ prs: [gitlab, github] }), "12")).toBe(github.url);
  });

  it("links a linked PR on any code host by its stored URL, and nothing that isn't a web URL", () => {
    const enterprise = {
      url: "https://ghe.example.com/acme/app/pull/7",
      host: "ghe.example.com",
      repository: "acme/app",
      number: 7,
      relationship: "attached" as const,
    };
    expect(linkedPullRequestUrl(info({ prs: [enterprise] }), "7")).toBe(enterprise.url);
    const scripted = { ...enterprise, url: "javascript:alert(1)" };
    expect(linkedPullRequestUrl(info({ prs: [scripted] }), "7")).toBeNull();
    expect(linkedPullRequestUrl(info({ prs: [enterprise] }), "07")).toBeNull();
  });
});

describe("pullRequestRefUrl", () => {
  it("links an explicit repo regardless of the workspace's, but only on github.com", () => {
    const ref = { repo: "other/lib", number: "5" };
    expect(pullRequestRefUrl(ref, info())).toBe("https://github.com/other/lib/pull/5");
    expect(pullRequestRefUrl(ref, undefined)).toBeNull();
    const ghe = { name_with_owner: "acme/app", host: "ghe.example.com" };
    expect(pullRequestRefUrl(ref, info({ workspace_repo: ghe }))).toBeNull();
    const gitlab = info({
      provider: "gitlab",
      workspace_repo: { name_with_owner: "acme/app", host: "gitlab.com" },
    });
    expect(pullRequestRefUrl(ref, gitlab)).toBeNull();
  });
});
