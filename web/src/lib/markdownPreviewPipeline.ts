// The remark and rehype plugins that render a Markdown file's preview, shared by
// every surface that shows one. Free of React and the DOM at runtime, so a worker can run it.

import { createMathPlugin } from "@streamdown/math";
import { rehypeGithubAlerts } from "rehype-github-alerts";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeSlug from "rehype-slug";
import remarkEmoji from "remark-emoji";
import remarkGfm from "remark-gfm";
import type { Options } from "react-markdown";
import { hasRawHtmlNode } from "@/shell/codeViewerHelpers";

// Same TeX math config as chat (see streamdown-security.ts): only `$$…$$` opens
// math so a lone `$` stays prose. Wraps remark-math + rehype-katex, whose plugin
// tuples slot straight into react-markdown, so the preview renders formulas the
// way the chat surface already does.
const MATH_PLUGIN = createMathPlugin({ singleDollarTextMath: false });

// GFM covers tables, task lists, strikethrough, and autolinks; remark-emoji
// renders GitHub-style `:shortcode:` emoji as their unicode glyphs so docs read
// the same here as on GitHub. remark-math parses `$$…$$` into math nodes.
export const MARKDOWN_PREVIEW_REMARK_PLUGINS: Options["remarkPlugins"] = [
  remarkGfm,
  remarkEmoji,
  MATH_PLUGIN.remarkPlugin,
];

// rehype-github-alerts turns `> [!NOTE]` blockquotes into GitHub's
// `<div class="markdown-alert markdown-alert-note">…` callout markup (GFM
// itself leaves them as plain blockquotes). We keep only the alert's div/p
// classes and drop its inline <svg> octicon in sanitize below; the icon is
// redrawn from CSS (see `.markdown-alert` in index.css) so the sanitized
// output stays a tiny, fixed class surface rather than arbitrary SVG.
const ALERT_CLASS = /^markdown-alert(-\w+)?$/;
const ALERT_TITLE_CLASS = /^markdown-alert-title$/;
// Extend the default (GitHub-derived) sanitize schema minimally: allow `class`
// on the alert wrapper div (markdown-alert*) and its title p (markdown-alert-
// title) only for those exact tokens. Everything else — <script>, event
// handlers, javascript: URLs, arbitrary classes — is still stripped, so raw
// HTML in a .md file stays safe to render inline. Keep hast-util-sanitize's
// default `clobberPrefix: "user-content-"`: heading IDs derive from untrusted
// markdown (rehype-slug), and the prefix keeps them from clobbering named DOM
// access or app-owned ids. The TOC reads `el.id` off the rendered DOM, so it
// works regardless of the prefix.
// Allow common img attributes (alt, width, height, align, valign) that GitHub supports.
export const MARKDOWN_PREVIEW_SANITIZE_SCHEMA = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    div: [...(defaultSchema.attributes?.div ?? []), ["className", ALERT_CLASS]],
    p: [...(defaultSchema.attributes?.p ?? []), ["className", ALERT_TITLE_CLASS]],
    img: [...(defaultSchema.attributes?.img ?? []), "alt", "width", "height", "align", "valign"],
  },
};

/** rehype-raw re-parses the whole tree through parse5, so skip it when there is no raw HTML. */
export function rehypeRawWhenPresent() {
  const transform = rehypeRaw();
  return (tree: Parameters<typeof transform>[0], file: Parameters<typeof transform>[1]) =>
    hasRawHtmlNode(tree) ? transform(tree, file) : tree;
}

// Markdown files routinely embed raw HTML that GitHub renders — <details>,
// <sub>/<sup>, <kbd>, <br>, <div align>, inline <img> — which react-markdown
// drops by default, showing the escaped tags as literal text. rehype-raw parses
// that HTML; rehype-sanitize then strips anything unsafe (<script>, event
// handlers, javascript: URLs) so this stays safe to render inline without an
// iframe. Order matters: alerts transform before sanitize, slug adds IDs to
// headings, and sanitize runs after raw parsing and GFM — only KaTeX comes
// later, rendering math from the already-sanitized tree.
export const MARKDOWN_PREVIEW_SANITIZED_REHYPE_PLUGINS = [
  rehypeRawWhenPresent,
  rehypeSlug,
  rehypeGithubAlerts,
  [rehypeSanitize, MARKDOWN_PREVIEW_SANITIZE_SCHEMA],
] satisfies Options["rehypePlugins"];

// After sanitize: sanitize neutralises the untrusted markdown/HTML, leaving the
// `language-math` code nodes as plain text; KaTeX then renders that trusted
// output. Running KaTeX before sanitize would strip its MathML/spans as unknown.
export const MARKDOWN_PREVIEW_MATH_REHYPE_PLUGIN = MATH_PLUGIN.rehypePlugin;

export const MARKDOWN_PREVIEW_REHYPE_PLUGINS: Options["rehypePlugins"] = [
  ...MARKDOWN_PREVIEW_SANITIZED_REHYPE_PLUGINS,
  MARKDOWN_PREVIEW_MATH_REHYPE_PLUGIN,
];
