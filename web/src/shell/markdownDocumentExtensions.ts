import type { AnyExtension } from "@tiptap/core";
import { StarterKit } from "@tiptap/starter-kit";
import { TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { ListItem, TaskItem } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import { CodeBlockWithLanguage } from "./TipTapCodeBlockExtension";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { SoftBreak } from "./TipTapSoftBreak";
import { BoundedTable, BoundedTaskList } from "./tiptapBoundedTokenizers";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";

// Parsed list items may start with a nested list, code block, quote, heading or
// table; `block+` keeps that doc schema-valid so the first transaction doesn't throw.
const SafeListItem = ListItem.extend({ content: "block+" });

/** Extensions that define how the Markdown viewer parses and renders a document. */
export function createMarkdownDocumentExtensions(
  conversationId: string,
  path: string,
): AnyExtension[] {
  return [
    // Disabled built-ins are replaced below; duplicate extension names would
    // shadow the GitHub-flavored, schema-safe and language-aware versions.
    StarterKit.configure({
      link: false,
      blockquote: false,
      listItem: false,
      codeBlock: false,
    }),
    SafeListItem,
    CodeBlockWithLanguage,
    // StarterKit lacks TaskList/TaskItem; these parse and round-trip GFM `- [ ]`
    // checkboxes, and `nested` lets Tab indent sub-checklists.
    BoundedTaskList,
    TaskItem.configure({ nested: true }),
    BoundedTable.configure({ resizable: true }),
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
