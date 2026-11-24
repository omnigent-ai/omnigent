// A newline inside a paragraph's markdown source: ProseMirror's break-spaces CSS
// would draw it as a line break, so hard-wrapped prose reads as short lines. This
// leaf renders a collapsible space; tiptapMarkdownPatches.ts splits/rejoins "\n".

import { Node, mergeAttributes } from "@tiptap/core";
import type { Mark } from "@tiptap/pm/model";
import type { Transaction } from "@tiptap/pm/state";

export const SOFT_BREAK_NODE = "softBreak";

/**
 * Turn the soft breaks of every textblock touching [from, to] back into "\n"
 * text. Needed before a block becomes text-only (a code block), where
 * ProseMirror would otherwise drop the leaves and join the wrapped lines.
 */
export function softBreaksToNewlines(tr: Transaction, from: number, to: number): void {
  const breaks: { pos: number; marks: readonly Mark[] }[] = [];
  tr.doc.nodesBetween(from, to, (node, pos) => {
    if (!node.isTextblock) return true;
    node.forEach((child, offset) => {
      if (child.type.name === SOFT_BREAK_NODE)
        breaks.push({ pos: pos + 1 + offset, marks: child.marks });
    });
    return false;
  });
  for (const { pos, marks } of breaks.reverse()) {
    tr.replaceWith(pos, pos + 1, tr.doc.type.schema.text("\n", marks));
  }
}

export const SoftBreak = Node.create({
  name: SOFT_BREAK_NODE,
  inline: true,
  group: "inline",
  selectable: false,

  parseHTML() {
    return [{ tag: "span[data-soft-break]" }];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, { "data-soft-break": "", class: "md-soft-break" }),
      " ",
    ];
  },

  renderText() {
    return "\n";
  },

  // ProseMirror's textBetween() reads leaf text from the schema spec, which
  // TipTap's renderText does not populate; both keep comment anchors and copied
  // text byte-identical to the raw file.
  extendNodeSchema(extension) {
    return extension.name === SOFT_BREAK_NODE ? { leafText: () => "\n" } : {};
  },

  // Fallback only: the serialiser patch rejoins soft breaks into text before
  // rendering so marks spanning a break are not closed and reopened around it.
  renderMarkdown: () => "\n",
});
