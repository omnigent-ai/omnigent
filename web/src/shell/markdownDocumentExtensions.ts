import type { AnyExtension } from "@tiptap/core";
import { StarterKit } from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { ListItem, TaskItem, TaskList } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import { CodeBlockWithLanguage } from "./TipTapCodeBlockExtension";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { SoftBreak } from "./TipTapSoftBreak";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";

// @tiptap/markdown parses a list item whose first child is a non-paragraph
// block — a nested list, a fenced code block, a blockquote, a heading, or a
// table — into a `listItem` that violates the stock `paragraph block*` content
// model. ProseMirror builds the initial doc via `nodeFromJSON`, which does NOT
// validate content, so the invalid doc loads silently; the first transaction
// that touches it (a user edit, or StarterKit's TrailingNode plugin on load)
// then calls `contentMatchAt` and throws ("Called contentMatchAt on a node
// with invalid content"), which the viewer's panel boundary catches — the
// whole file view crashes instead of rendering. Relaxing the content model to
// `block+` accepts a non-paragraph first child, keeping the parsed doc
// schema-valid. Same class as the blockquote fix in #2004 (see toBlockContent),
// for list items.
const SafeListItem = ListItem.extend({ content: "block+" });

/** Extensions that define how the Markdown viewer parses and renders a document. */
export function createMarkdownDocumentExtensions(
  conversationId: string,
  path: string,
): AnyExtension[] {
  return [
    // link/blockquote: false — StarterKit bundles its own versions whose
    // markdown handlers would shadow the GitHub-flavored replacements
    // below (duplicate extension names: first wins).
    // listItem: false — replaced by SafeListItem (content: block+) so a list
    // item whose first child is a non-paragraph block doesn't build a
    // schema-invalid doc that crashes the panel. See SafeListItem above.
    // codeBlock: false — replaced by CodeBlockWithLanguage, which adds a
    // language picker and a live mermaid preview via a React node view.
    StarterKit.configure({
      link: false,
      blockquote: false,
      listItem: false,
      codeBlock: false,
    }),
    SafeListItem,
    CodeBlockWithLanguage,
    // Task lists (GitHub `- [ ]` / `- [x]`). StarterKit ships
    // BulletList/OrderedList/ListItem but NOT TaskList/TaskItem, so without
    // these two the markdown parser drops the checkbox and renders a plain
    // bullet. Registering them (a) makes @tiptap/markdown pick up TaskList's
    // built-in `- [ ]` marked tokenizer so the syntax parses into checkbox
    // items, and (b) round-trips back to identical markdown via their
    // built-in renderMarkdown. nested:true lets Tab indent sub-checklists.
    TaskList,
    TaskItem.configure({ nested: true }),
    Table.configure({ resizable: true }),
    TableRow,
    TableCell,
    TableHeader,
    ImageAwareLink.configure({ openOnClick: false, autolink: false }),
    GitHubAlertBlockquote,
    HtmlPassthrough,
    // Soft line breaks in hard-wrapped source render as spaces so Edit mode
    // reflows prose to the viewer width like Preview; see TipTapSoftBreak.ts.
    SoftBreak,
    Markdown,
    createWorkspaceImageExtension(conversationId, path),
  ];
}
