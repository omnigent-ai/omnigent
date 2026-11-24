// CodeBlock with a React node view (language selector + live mermaid preview),
// registered in place of StarterKit's built-in codeBlock. Extending the base
// keeps its `language` attribute and markdown handlers, so the fence language
// still round-trips through markdown unchanged.

import { InputRule } from "@tiptap/core";
import { CodeBlock } from "@tiptap/extension-code-block";
import { ReactNodeViewRenderer } from "@tiptap/react";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";
import { softBreaksToNewlines } from "./TipTapSoftBreak";
import { fenceForBody } from "./markdownFence";

export const CodeBlockWithLanguage = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(TipTapCodeBlockView);
  },

  // A code block holds text only, so converting a hard-wrapped paragraph would
  // drop its softBreak leaves and join the lines; restore them as "\n" first.
  // The transaction is dispatched even when a command fails, so check first.
  addCommands() {
    return {
      ...this.parent?.(),
      setCodeBlock:
        (attributes) =>
        ({ tr, state, commands, can }) => {
          if (!can().setNode(this.name, attributes)) return false;
          softBreaksToNewlines(tr, state.selection.from, state.selection.to);
          return commands.setNode(this.name, attributes);
        },
      toggleCodeBlock:
        (attributes) =>
        ({ tr, state, commands, can }) => {
          if (!can().toggleNode(this.name, "paragraph", attributes)) return false;
          softBreaksToNewlines(tr, state.selection.from, state.selection.to);
          return commands.toggleNode(this.name, "paragraph", attributes);
        },
    };
  },

  addInputRules() {
    return (this.parent?.() ?? []).map(
      (rule) =>
        new InputRule({
          find: rule.find,
          undoable: rule.undoable,
          handler: (props) => {
            const $from = props.state.doc.resolve(props.range.from);
            softBreaksToNewlines(props.state.tr, $from.start(), $from.end());
            return rule.handler(props);
          },
        }),
    );
  },

  // The base serializer emits a fixed ``` fence, which corrupts a code block
  // whose body contains a ``` line (only representable with a longer outer
  // fence). Emit a fence longer than any backtick run in the body instead;
  // this is byte-identical to the base for backtick-free content.
  renderMarkdown(node, helpers) {
    const language = (node.attrs?.language as string | null) ?? "";
    const body = helpers.renderChildren(node.content ?? []);
    const fence = fenceForBody(body);
    return `${fence}${language}\n${body}\n${fence}`;
  },
});
