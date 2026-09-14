// CodeBlock with a React node view (language selector + live mermaid preview),
// registered in place of StarterKit's built-in codeBlock. Extending the base
// keeps its `language` attribute and markdown handlers, so the fence language
// still round-trips through markdown unchanged.

import { CodeBlock } from "@tiptap/extension-code-block";
import { ReactNodeViewRenderer } from "@tiptap/react";
import { TipTapCodeBlockView } from "./TipTapCodeBlockView";

export const CodeBlockWithLanguage = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(TipTapCodeBlockView);
  },
});
