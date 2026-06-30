/**
 * Unit tests for the GitHub-alert-aware Blockquote:
 *   - extractAlert marker detection/stripping on parsed children
 *   - alert markdown parses into a blockquote with alertType + DOM hooks,
 *     with the marker hidden from the visible content
 *   - serialisation re-emits the `> [!NOTE]` marker byte-faithfully
 *   - plain blockquotes are untouched in both directions
 *
 * Editor tests use a real TipTap Editor (real schema, real @tiptap/markdown
 * parsing) so regressions in parse/serialise behaviour fail the test.
 */

import { afterEach, describe, expect, it } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import { extractAlert, GitHubAlertBlockquote } from "./TipTapGitHubAlert";

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
});

/** Mounted editor matching the viewer's blockquote configuration. */
function makeEditor(markdown: string): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      // Same as MarkdownRichTextViewer: StarterKit's bundled Blockquote is
      // disabled so GitHubAlertBlockquote owns the markdown token.
      StarterKit.configure({ blockquote: false }),
      GitHubAlertBlockquote,
      Markdown,
    ],
    content: markdown,
    contentType: "markdown",
  });
}

// ---------------------------------------------------------------------------
// extractAlert
// ---------------------------------------------------------------------------

describe("extractAlert", () => {
  it("strips an inline marker from the first paragraph", () => {
    const { alertType, children } = extractAlert([
      { type: "paragraph", content: [{ type: "text", text: "[!NOTE]\nbody text" }] },
    ]);
    expect(alertType).toBe("note");
    expect(children[0].content![0].text).toBe("body text");
  });

  it("drops a marker-only paragraph but keeps the rest", () => {
    const { alertType, children } = extractAlert([
      { type: "paragraph", content: [{ type: "text", text: "[!WARNING]" }] },
      { type: "paragraph", content: [{ type: "text", text: "body" }] },
    ]);
    expect(alertType).toBe("warning");
    expect(children).toHaveLength(1);
    expect(children[0].content![0].text).toBe("body");
  });

  it("keeps at least one paragraph when the marker was the only content", () => {
    const { alertType, children } = extractAlert([
      { type: "paragraph", content: [{ type: "text", text: "[!TIP]" }] },
    ]);
    expect(alertType).toBe("tip");
    expect(children).toEqual([{ type: "paragraph" }]);
  });

  it("ignores non-marker quotes and mid-text markers", () => {
    expect(
      extractAlert([{ type: "paragraph", content: [{ type: "text", text: "just a quote" }] }])
        .alertType,
    ).toBeNull();
    expect(
      extractAlert([{ type: "paragraph", content: [{ type: "text", text: "see [!NOTE] inline" }] }])
        .alertType,
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Editor parse + render + round-trip
// ---------------------------------------------------------------------------

const NOTE_MD =
  "> [!NOTE]\n> Teammates need to reach the server.\n> A local server is network-only.";

describe("GitHubAlertBlockquote", () => {
  it("parses an alert into a styled blockquote with the marker hidden", () => {
    editor = makeEditor(NOTE_MD);
    const quote = editor.view.dom.querySelector("blockquote");
    expect(quote).not.toBeNull();
    // DOM hooks consumed by the alert CSS in index.css.
    expect(quote!.getAttribute("data-alert-type")).toBe("note");
    expect(quote!.getAttribute("data-alert-label")).toBe("Note");
    // The marker itself is not part of the visible content.
    expect(quote!.textContent).not.toContain("[!NOTE]");
    expect(quote!.textContent).toContain("Teammates need to reach the server.");
  });

  it("round-trips the alert markdown byte-faithfully", () => {
    editor = makeEditor(NOTE_MD);
    expect(editor.getMarkdown().trim()).toBe(NOTE_MD);
  });

  it("supports all five GitHub alert types", () => {
    for (const [marker, label] of [
      ["NOTE", "Note"],
      ["TIP", "Tip"],
      ["IMPORTANT", "Important"],
      ["WARNING", "Warning"],
      ["CAUTION", "Caution"],
    ]) {
      const ed = makeEditor(`> [!${marker}]\n> body`);
      const quote = ed.view.dom.querySelector("blockquote")!;
      expect(quote.getAttribute("data-alert-type")).toBe(marker.toLowerCase());
      expect(quote.getAttribute("data-alert-label")).toBe(label);
      expect(ed.getMarkdown().trim()).toBe(`> [!${marker}]\n> body`);
      ed.destroy();
    }
  });

  it("leaves plain blockquotes untouched in both directions", () => {
    const md = "> an ordinary quote\n> with two lines";
    editor = makeEditor(md);
    const quote = editor.view.dom.querySelector("blockquote")!;
    expect(quote.getAttribute("data-alert-type")).toBeNull();
    expect(editor.getMarkdown().trim()).toBe(md);
  });
});
