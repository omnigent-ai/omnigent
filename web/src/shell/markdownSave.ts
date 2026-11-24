import type { Editor } from "@tiptap/core";
import { markdownKeepingSource } from "./tiptapMarkdownPatches";

/**
 * The file contents Save writes for the Markdown editor's document.
 *
 * Unedited blocks keep their source (markdownSourceBlocks.ts). TipTap drops the
 * final newline and saves its trailing empty paragraph as blank lines, so the
 * file ends the way it was loaded: with one newline, or none.
 *
 * :param editor: The Markdown editor.
 * :param endsWithNewline: Whether the loaded file ended with a newline.
 */
export function savedMarkdown(editor: Editor, endsWithNewline: boolean): string {
  const markdown = markdownKeepingSource(editor).replace(/\n+$/, "");
  return endsWithNewline && markdown !== "" ? `${markdown}\n` : markdown;
}
