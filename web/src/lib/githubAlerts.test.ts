import type { Root } from "hast";
import { toHtml } from "hast-util-to-html";
import rehypeRaw from "rehype-raw";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import remarkRehype from "remark-rehype";
import { unified } from "unified";
import { describe, expect, it } from "vitest";

import { rehypeGitHubAlerts } from "./githubAlerts";

const processor = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkRehype, { allowDangerousHtml: true })
  .use(rehypeRaw)
  .use(rehypeGitHubAlerts);
const html = (markdown: string) => toHtml(processor.runSync(processor.parse(markdown)) as Root);
const alertKinds = (markdown: string) =>
  [...html(markdown).matchAll(/markdown-alert-(note|tip|important|warning|caution)/g)].map(
    (match) => match[1],
  );

describe("rehypeGitHubAlerts", () => {
  it.each(["NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION", "note"])(
    "turns a [!%s] quote into its alert",
    (kind) => {
      expect(alertKinds(`> [!${kind}]\n> Body`)).toEqual([kind.toLowerCase()]);
    },
  );

  it.each([
    ["text on the marker's line", "> [!NOTE] Same line"],
    ["a marker with nothing after it", "> [!NOTE]"],
    ["an unknown kind", "> [!DANGER]\n> Body"],
    ["a marker after the first block", "> Intro\n>\n> [!NOTE]\n> Body"],
    ["emphasis on the marker's line", "> [!NOTE] **Same line**"],
    ["code on the marker's line", "> [!NOTE] `Same line`"],
    ["a link on the marker's line", "> [!NOTE] [Same line](https://example.com)"],
  ])("leaves %s as a quote", (_label, markdown) => {
    expect(alertKinds(markdown)).toEqual([]);
    expect(html(markdown)).toContain("<blockquote>");
  });

  it.each([
    ["emphasis", "> [!NOTE]\n> **Next line**", "<strong>Next line</strong>"],
    ["code", "> [!NOTE]\n> `Next line`", "<code>Next line</code>"],
    ["a link", "> [!NOTE]\n> [Next line](https://example.com)", ">Next line</a>"],
    ["a line break", "> [!NOTE]\n> <br>Body", "<p><br>Body</p>"],
  ])("keeps %s that opens the next line in the alert", (_label, markdown, kept) => {
    expect(alertKinds(markdown)).toEqual(["note"]);
    expect(html(markdown)).toContain(kept);
  });

  it("keeps the blocks after the first paragraph, and leaves a nested marker as a quote", () => {
    const rendered = html("> [!TIP]\n> First\n>\n> - item\n>\n> > [!CAUTION]\n> > Inner");
    expect(rendered).toContain('<p class="markdown-alert-title">Tip</p>');
    expect(rendered).toContain("<p>First</p>");
    expect(rendered).toMatch(/<li>\s*(<p>)?item/);
    expect(alertKinds("> [!TIP]\n> First\n>\n> > [!CAUTION]\n> > Inner")).toEqual(["tip"]);
    expect(rendered).toContain("[!CAUTION]");
  });

  it("renders alerts only at the top level, as GitHub does", () => {
    expect(alertKinds("> Quote\n>\n> > [!NOTE]\n> > Inner")).toEqual([]);
    expect(alertKinds("- item\n\n  > [!WARNING]\n  > Inside a list")).toEqual([]);
  });
});
