// Byte-preserving passthrough for raw HTML blocks in markdown files.
//
// Without this, @tiptap/markdown drops any block-level HTML it can't map
// into the schema: `<details>` / `<summary>` wrappers, `<!-- comments -->`,
// and `<div align="center">` wrappers all silently vanish from the file on
// the first save after an edit. GitHub keeps raw HTML blocks verbatim.
//
// This node claims block-level `html` tokens at parse time, stores the raw
// source in an attr, and serialises it back byte-for-byte. Rendering is
// deliberately conservative — the raw HTML is never injected into the DOM
// (a shared session could otherwise smuggle active content through a
// workspace file). Instead:
//   - `<summary>` blocks render their text as a dim disclosure line
//   - everything else (comments, bare open/close wrapper tags) renders as a
//     zero-height placeholder
//
// One deliberate exception: blocks containing `<img` are NOT claimed — they
// fall through to the default HTML parsing so images keep rendering through
// the workspace-aware image pipeline (at the cost of losing the surrounding
// wrapper markup, e.g. a `<p align="center">` around a hero image).

import { Node, type MarkdownParseResult } from "@tiptap/core";

/** Matches a block whose raw HTML includes an <img …> tag (any case). */
const CONTAINS_IMG = /<img[\s>]/i;

/** Extracts the inner text of the first <summary> element, if present. */
const SUMMARY_TEXT = /<summary[^>]*>([^<]*)<\/summary>/i;

export const HtmlPassthrough = Node.create({
  name: "htmlPassthrough",
  group: "block",
  atom: true,
  // Raw blocks are workspace bytes, not editable rich content — selectable
  // (so they can be deleted) but with no editable interior.
  selectable: true,

  addAttributes() {
    return {
      // The exact source bytes of the HTML block, trailing newlines trimmed
      // (the serialiser re-joins blocks with blank lines).
      raw: { default: "", rendered: false },
    };
  },

  parseHTML() {
    // Never created from pasted HTML — only from markdown parse below.
    return [];
  },

  renderHTML({ node }) {
    const raw: string = node.attrs.raw ?? "";
    const summary = SUMMARY_TEXT.exec(raw);
    if (summary) {
      return [
        "div",
        { "data-html-passthrough": "summary", class: "md-html-passthrough-summary" },
        // Text only — the raw HTML itself is never injected.
        `▸ ${summary[1].trim()}`,
      ];
    }
    return ["div", { "data-html-passthrough": "hidden", class: "md-html-passthrough-hidden" }];
  },

  parseMarkdown: (token, helpers) => {
    const raw = (token.raw ?? token.text ?? "").toString();
    // Image-bearing blocks fall through so they render via the image
    // extension. The runtime treats a falsy parse result as "decline, try
    // the next handler / default HTML parse", but the upstream type doesn't
    // model declining — hence the cast.
    if (CONTAINS_IMG.test(raw)) return null as unknown as MarkdownParseResult;
    return helpers.createNode("htmlPassthrough", { raw: raw.replace(/\n+$/, "") }, []);
  },
  markdownTokenName: "html",

  renderMarkdown: (node) => (node.attrs?.raw as string | undefined) ?? "",
});
