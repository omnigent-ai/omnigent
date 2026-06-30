/**
 * Round-trip preservation tests for the markdown editor's full extension
 * stack: the HtmlPassthrough node (raw HTML blocks survive a save), the
 * minimal-escaping serialiser patch (no &amp;/&lt; churn), and the image
 * attribute preservation (alt=""/valign/align on raw <img> tags).
 *
 * These guard against the editor REWRITING file content it doesn't model —
 * the failure mode where opening a README and making any edit silently
 * drops <details> wrappers, <!-- comments -->, and escapes every "&".
 * Each test uses a real TipTap editor with the exact extension stack the
 * viewer mounts (only the HTTP boundary is mocked).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { installMarkdownSerializerPatch } from "./tiptapMarkdownPatches";

vi.mock("@/hooks/useFileContent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useFileContent")>();
  return { ...actual, fetchFileContent: vi.fn().mockResolvedValue(new Promise(() => {})) };
});

installMarkdownSerializerPatch();

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
});

/** Mounted editor with the SAME extension stack as MarkdownRichTextViewer. */
function makeEditor(markdown: string): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      StarterKit.configure({ link: false, blockquote: false }),
      Table.configure({ resizable: true }),
      TableRow,
      TableCell,
      TableHeader,
      ImageAwareLink.configure({ openOnClick: false, autolink: false }),
      GitHubAlertBlockquote,
      HtmlPassthrough,
      Markdown,
      createWorkspaceImageExtension("conv_test1", "README.md"),
    ],
    content: markdown,
    contentType: "markdown",
  });
}

/** Parse + serialise, normalising only the trailing newline. */
function roundTrip(markdown: string): string {
  editor = makeEditor(markdown);
  const out = editor.getMarkdown().trim();
  editor.destroy();
  editor = null;
  return out;
}

describe("HtmlPassthrough", () => {
  it("preserves <details>/<summary> wrappers around markdown content", () => {
    const md = [
      "<details>",
      "<summary>Prefer to install manually?</summary>",
      "",
      "Some **markdown** body.",
      "",
      "</details>",
    ].join("\n");
    expect(roundTrip(md)).toBe(md);
  });

  it("preserves HTML comments", () => {
    const md = "before\n\n<!-- TODO: screenshot of Admin → Members → Invite. -->\n\nafter";
    expect(roundTrip(md)).toBe(md);
  });

  it("preserves <div align> wrappers", () => {
    const md = '<div align="center">\n\n# Heading\n\n</div>';
    expect(roundTrip(md)).toBe(md);
  });

  it("renders a summary block as a visible disclosure line, comments as nothing", () => {
    editor = makeEditor("<details>\n<summary>Gateway base URLs</summary>\n\nbody\n\n</details>");
    const summary = editor.view.dom.querySelector('[data-html-passthrough="summary"]');
    expect(summary?.textContent).toContain("Gateway base URLs");
    // The raw HTML must NOT be injected into the DOM (no real <details> tag).
    expect(editor.view.dom.querySelector("details")).toBeNull();
  });

  it("leaves <img>-bearing HTML blocks to the image pipeline", () => {
    editor = makeEditor('<img src="docs/hero.png" alt="hero" width="520" />');
    // Parsed as an image node (renders through the workspace pipeline)…
    expect(editor.view.dom.querySelector("img")).not.toBeNull();
    // …and not swallowed into a passthrough block.
    expect(editor.view.dom.querySelector("[data-html-passthrough]")).toBeNull();
  });
});

describe("serializer escaping patch", () => {
  it("round-trips & and < in prose without entity churn", () => {
    expect(roundTrip("### Choose & switch models")).toBe("### Choose & switch models");
    expect(roundTrip("AT&T and R&D")).toBe("AT&T and R&D");
  });

  it("still escapes sequences that would change meaning on re-parse", () => {
    // Source "&amp;" DISPLAYS as "&"; serialising the simpler "&" is
    // display-identical and must then be stable on subsequent round-trips.
    expect(roundTrip("escape &amp; entity")).toBe("escape & entity");
    expect(roundTrip("escape & entity")).toBe("escape & entity");
    // Text that displays a literal "&amp;" must keep its escaped form, or it
    // would decode to "&" on the next open.
    const displaysEntity = roundTrip("show &amp;amp; verbatim");
    editor = makeEditor(displaysEntity);
    expect(editor.getText()).toContain("&amp;");
    editor.destroy();
    editor = null;
    // Tag-like "<b>" in prose must not become live HTML on the way back in.
    const tagLike = roundTrip("a \\<b> tag");
    editor = makeEditor(tagLike);
    expect(editor.view.dom.querySelector("b")).toBeNull();
  });

  it("does not escape inside code spans and fences", () => {
    expect(roundTrip("run `a && b` now")).toBe("run `a && b` now");
    expect(roundTrip("```bash\na && b < c\n```")).toBe("```bash\na && b < c\n```");
  });
});

describe("image attribute preservation", () => {
  it('keeps alt="", height, and valign on raw <img> tags byte-for-byte', () => {
    const md = '# <img src="docs/images/logo.svg" alt="" height="38" valign="middle" /> Omnigent';
    expect(roundTrip(md)).toBe(md);
  });
});
