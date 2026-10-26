import { getSchema } from "@tiptap/core";
import { MarkdownManager } from "@tiptap/markdown";
import type { Node } from "@tiptap/pm/model";
import type { Comment } from "@/hooks/useComments";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { installMarkdownParserPatch } from "./tiptapMarkdownPatches";
import { remapMarkdownComment } from "./TipTapEditorHelpers";
import { isMarkdownTooLargeForRichView } from "./codeViewerHelpers";

installMarkdownParserPatch();
const extensions = createMarkdownDocumentExtensions("", "");
const manager = new MarkdownManager({ extensions });
const schema = getSchema(extensions);
let previous: { content: string; doc: Node | null } | undefined;

export function remapFormattedMarkdownComment(comment: Comment, content: string): Comment {
  if (previous?.content !== content) {
    previous = { content, doc: null };
    try {
      if (!isMarkdownTooLargeForRichView("markdown", content)) {
        previous.doc = schema.nodeFromJSON(manager.parse(content));
      }
    } catch (error) {
      console.warn("Markdown comment remapping failed; keeping stored offsets", error);
    }
  }
  try {
    return previous.doc ? remapMarkdownComment(previous.doc, comment, content, manager) : comment;
  } catch (error) {
    console.warn("Markdown comment remapping failed; keeping stored offsets", error);
    return comment;
  }
}
