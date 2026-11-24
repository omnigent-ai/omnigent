// Copying from the rich Markdown editor writes Markdown as the plain-text
// flavor, so a paste into a terminal, a chat box or a code editor keeps headings,
// emphasis, links and lists. The HTML flavor is left to ProseMirror, so rich-text
// targets and the editor itself still paste formatted content.

import { Extension, type Editor, type JSONContent } from "@tiptap/core";
import type { Slice } from "@tiptap/pm/model";
import { Plugin, PluginKey, type Selection } from "@tiptap/pm/state";
import { rememberedMarkdown } from "./markdownSourceBlocks";
import { markdownKeepingSource } from "./tiptapMarkdownPatches";

function trimNewlines(markdown: string): string {
  return markdown.replace(/^\n+|\n+$/g, "");
}

/** *markdown* without the line endings and edge blank lines the editor can't hold. */
function withoutEdges(markdown: string): string {
  return markdown.replace(/\r\n?/g, "\n").replace(/^\n+|\s+$/g, "");
}

// Unchanged, the document copies as the loaded file, byte for byte; once edited,
// it copies as Save would write it.
function wholeDocumentMarkdown(editor: Editor): string {
  const markdown = trimNewlines(markdownKeepingSource(editor));
  const loaded = rememberedMarkdown(editor);
  if (loaded === null) return markdown;
  if (withoutEdges(loaded) === withoutEdges(markdown)) return loaded;
  return loaded.endsWith("\n") && markdown ? `${markdown}\n` : markdown;
}

/**
 * The Markdown to put on the clipboard for copying *slice*, the content of *selection*.
 *
 * Part of one block, or one table cell, copies as inline Markdown (raw text
 * inside a code block); whole blocks keep their block syntax; the whole
 * document copies as its source.
 *
 * :returns: The Markdown, or "" to fall back to the editor's plain text.
 */
export function copiedMarkdown(editor: Editor, slice: Slice, selection: Selection): string {
  const manager = editor.markdown;
  if (!manager) return "";
  try {
    const { doc } = selection.$from;
    if (slice.content.eq(doc.content)) return wholeDocumentMarkdown(editor);
    const { $from, $to } = selection;
    const block = $from.parent;
    // A whole cell is still just its text; table syntax needs whole rows.
    const inTableCell = $from.depth > 1 && /cell/.test($from.node(-1).type.spec.tableRole ?? "");
    const inlineOnly =
      block.isTextblock &&
      $from.sameParent($to) &&
      (inTableCell || $from.parentOffset > 0 || $to.parentOffset < block.content.size);
    if (inlineOnly) {
      if (block.type.spec.code) return doc.textBetween($from.pos, $to.pos);
      // Without parents the slice is just the selected inline nodes.
      const inline = doc.slice($from.pos, $to.pos).content.toJSON() ?? [];
      const paragraph: JSONContent = { type: "paragraph", content: inline };
      return trimNewlines(manager.serialize({ type: "doc", content: [paragraph] }));
    }
    return trimNewlines(manager.serialize({ type: "doc", content: slice.content.toJSON() ?? [] }));
  } catch {
    return "";
  }
}

export const MarkdownClipboard = Extension.create({
  name: "markdownClipboard",

  addProseMirrorPlugins() {
    const { editor } = this;
    return [
      new Plugin({
        key: new PluginKey("markdownClipboard"),
        props: {
          clipboardTextSerializer: (slice, view) =>
            copiedMarkdown(editor, slice, view.state.selection),
        },
      }),
    ];
  },
});
