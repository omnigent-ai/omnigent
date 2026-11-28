import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { describe, expect, it } from "vitest";

import rehypeRaw from "rehype-raw";
import type { Root as HastRoot } from "hast";
import { toHtml } from "hast-util-to-html";
import remarkRehype from "remark-rehype";

import {
  githubImageEmojiUrl,
  rehypeGitHubEmoji,
  remarkGitHubEmoji,
  splitGitHubEmoji,
} from "./githubEmojiShortcodes";

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
}

const processor = unified().use(remarkParse).use(remarkGfm).use(remarkGitHubEmoji);

/** Every leaf's type and text, in order, as the renderer will see them. */
function leaves(markdown: string): [string, string][] {
  const tree = processor.runSync(processor.parse(markdown)) as MarkdownNode;
  const found: [string, string][] = [];
  const walk = (node: MarkdownNode) => {
    if (typeof node.value === "string") found.push([node.type, node.value]);
    for (const child of node.children ?? []) walk(child);
  };
  walk(tree);
  return found;
}

const text = (markdown: string) =>
  leaves(markdown)
    .map(([, value]) => value)
    .join("");

describe("remarkGitHubEmoji", () => {
  it.each([
    [":green_book: Docs :+1: :-1: :tada:", "📗 Docs 👍 👎 🎉"],
    ["**Done** :white_check_mark:", "Done ✅"],
    // Unknown names, and GitHub's image-only ones, stay as typed.
    [":not_an_emoji: and :shipit:", ":not_an_emoji: and :shipit:"],
    ["time 10:30:45 stays", "time 10:30:45 stays"],
  ])("renders %j as %j", (markdown, expected) => {
    expect(text(markdown)).toBe(expected);
  });

  it("leaves code spans, fenced code and indented code as typed", () => {
    expect(leaves("Run `:tada:` now")).toContainEqual(["inlineCode", ":tada:"]);
    expect(leaves("```\n:tada:\n```")).toEqual([["code", ":tada:"]]);
    expect(leaves("Text\n\n    :tada: indented\n")).toContainEqual(["code", ":tada: indented"]);
  });

  it("leaves the text in inline HTML code as typed, and turns the text around it", () => {
    expect(text("<code>:tada:</code> then <kbd>:+1:</kbd> :tada:")).toBe(
      "<code>:tada:</code> then <kbd>:+1:</kbd> 🎉",
    );
  });

  // Excerpts see raw HTML as unparsed text, so only a plain tag pair within one paragraph is
  // recognized; the rendered page (the rehype plugin) parses HTML and keeps all of these as typed.
  it("turns shortcodes in raw-HTML code it can't recognize, a known limit of excerpts", () => {
    expect(text('<code title="a > b">:tada:</code>')).toBe('<code title="a > b">🎉</code>');
    // Inline code that spans two paragraphs is closed in another parent than the one it opens in.
    expect(text("Run <code>:tada:\n\n:tada:</code>")).toBe("Run <code>:tada:🎉</code>");
    // A block of raw HTML, even across blank lines, is one node and stays as typed.
    expect(text("<pre>\n:tada:\n\n:tada:\n</pre>")).toBe("<pre>\n:tada:\n\n:tada:\n</pre>");
  });

  it("renders link text, but never a bare URL or address GitHub linked by itself", () => {
    expect(text("[ship :rocket:](https://example.com/:rocket:)")).toBe("ship 🚀");
    expect(leaves("See https://example.com/a/:tada:/b now")).toContainEqual([
      "text",
      "https://example.com/a/:tada:/b",
    ]);
    expect(leaves("Or www.example.com/:tada:/x here")).toContainEqual([
      "text",
      "www.example.com/:tada:/x",
    ]);
    // The URL itself is never touched, even where its link text is.
    const tree = processor.runSync(processor.parse("[x :tada:](https://example.com/:tada:)"));
    const link = (tree as MarkdownNode).children![0]!.children![0]!;
    expect(link.url).toBe("https://example.com/:tada:");
  });
});

describe("splitGitHubEmoji", () => {
  it.each([
    // GitHub's names and aliases, including ones only GitHub's list has.
    [":hourglass_flowing_sand: :pushpin: :thumbsup: :+1: :satisfied:", "⏳ 📌 👍 👍 😆"],
    // A closing colon can open the next shortcode.
    ["10:30:+1:", "10:30👍"],
    [":Smile: :not_an_emoji:", ":Smile: :not_an_emoji:"],
  ])("renders %j as %j", (input, expected) => {
    expect(splitGitHubEmoji(input)).toEqual([{ text: expected }]);
  });

  it("sets GitHub's picture emoji apart from the text around them", () => {
    expect(splitGitHubEmoji("ship :shipit: it :tada:")).toEqual([
      { text: "ship " },
      { image: "shipit" },
      { text: " it 🎉" },
    ]);
    expect(splitGitHubEmoji(":octocat:")).toEqual([{ image: "octocat" }]);
  });
});

describe("rehypeGitHubEmoji", () => {
  const htmlProcessor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkRehype, { allowDangerousHtml: true })
    .use(rehypeRaw)
    .use(rehypeGitHubEmoji);
  const html = (markdown: string) =>
    toHtml(htmlProcessor.runSync(htmlProcessor.parse(markdown)) as HastRoot);

  it("renders shortcodes in raw HTML as in Markdown", () => {
    expect(html("<p>:hourglass_flowing_sand: Waiting</p>\n\n**:pushpin:** pinned")).toBe(
      "<p>⏳ Waiting</p>\n<p><strong>📌</strong> pinned</p>",
    );
  });

  it("leaves code, keyboard input and autolinked URLs as typed", () => {
    expect(html("`:tada:` <kbd>:tada:</kbd> <pre>:tada:</pre>")).not.toContain("🎉");
    expect(html("See https://example.com/:tada:/b")).toContain("https://example.com/:tada:/b</a>");
    expect(html("See https://example.com/é/:shipit:/b")).toContain(
      ">https://example.com/é/:shipit:/b</a>",
    );
    expect(html("See https://example.com/é/%20/:tada:/b")).toContain(
      ">https://example.com/é/%20/:tada:/b</a>",
    );
    expect(html("[ship :rocket:](https://example.com/:rocket:)")).toContain(">ship 🚀</a>");
  });

  it("draws GitHub's picture emoji as sized images from GitHub, with their shortcode as the text", () => {
    expect(html("ship :shipit:")).toBe(
      `<p>ship <img class="github-emoji-image" src="${githubImageEmojiUrl("shipit")}" alt=":shipit:" title=":shipit:" style="width: 1.25em; height: 1.25em; vertical-align: -0.2em"></p>`,
    );
  });
});
