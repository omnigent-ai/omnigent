import { getSchema, type JSONContent } from "@tiptap/core";
import { DOMParser } from "@tiptap/pm/model";
import { MarkdownManager } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import { Table, TableRow, TableCell, TableHeader } from "@tiptap/extension-table";
import { CodeBlock } from "@tiptap/extension-code-block";
import { fenceForBody } from "@/shell/markdownFence";

const ClipboardCodeBlock = CodeBlock.extend({
  renderMarkdown(node, helpers) {
    // Rendered Markdown adds one newline inside <code>; the closing fence supplies it.
    const body = helpers.renderChildren(node.content ?? []).replace(/\n$/, "");
    const fence = fenceForBody(body);
    return `${fence}${node.attrs?.language ?? ""}\n${body}\n${fence}`;
  },
});
const extensions = [
  StarterKit.configure({ codeBlock: false }),
  ClipboardCodeBlock,
  Table,
  TableRow,
  TableCell,
  TableHeader,
];
const markdown = new MarkdownManager({ extensions });
const parser = DOMParser.fromSchema(getSchema(extensions));

/** Convert clipboard HTML without mounting it or loading its remote resources. */
export function clipboardHtmlToMarkdown(html: string): string | null {
  if (!html) return null;
  try {
    const template = document.createElement("template");
    template.innerHTML = html;
    template.content
      .querySelectorAll("script, style, iframe, object, embed")
      .forEach((node) => node.remove());
    const content: JSONContent = parser.parse(template.content).toJSON();
    // Plain wrappers (including syntax-highlighted source) should keep text/plain verbatim.
    const formatted = content.content?.some(
      (node) =>
        node.type !== "paragraph" ||
        node.content?.some((child) => child.type !== "text" || child.marks?.length),
    );
    return formatted ? markdown.serialize(content).trim() || null : null;
  } catch {
    // Conversion must never cost the user their plain-text paste.
    return null;
  }
}
