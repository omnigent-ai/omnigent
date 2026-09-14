// CodeBlock with a React node view (language selector + live mermaid preview),
// registered in place of StarterKit's built-in codeBlock. Extending the base
// keeps its `language` attribute and markdown handlers, so the fence language
// still round-trips through markdown unchanged.

import { CodeBlock } from "@tiptap/extension-code-block";
import { ReactNodeViewRenderer } from "@tiptap/react";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";
import { fenceForBody } from "./markdownFence";

export const CodeBlockWithLanguage = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(TipTapCodeBlockView);
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
