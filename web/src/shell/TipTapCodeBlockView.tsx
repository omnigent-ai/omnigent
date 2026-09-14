// Node view for markdown code blocks in the rich-text editor: the fenced source
// stays editable, a <select> picks the fence language, and a `mermaid` block
// renders a live read-only diagram below the source.

import { useCallback, useEffect, useMemo, useState } from "react";
import { NodeViewContent, NodeViewWrapper, type NodeViewProps } from "@tiptap/react";
import { MermaidPreview } from "./MermaidPreview";
import { CODE_BLOCK_LANGUAGES } from "./codeBlockLanguages";

export function TipTapCodeBlockView({ node, updateAttributes, editor }: NodeViewProps) {
  const language = (node.attrs.language as string | null) ?? "";
  // Hand-authored fences may be cased (```Mermaid); match case-insensitively.
  const isMermaid = language.toLowerCase() === "mermaid";
  const source = node.textContent;

  // Re-rendering the diagram on every keystroke is janky for a large graph and
  // repeatedly trips the error boundary on half-typed source. Let the preview
  // settle after the user pauses typing.
  const [previewSource, setPreviewSource] = useState(source);
  useEffect(() => {
    const timer = setTimeout(() => setPreviewSource(source), 300);
    return () => clearTimeout(timer);
  }, [source]);

  // The editor is read-only for a file the user can't edit; the language
  // picker must not offer to mutate the doc in that case.
  const editable = editor.isEditable;

  // Surface a language not in the quick-pick list (e.g. ```rust) as the current
  // option so the picker shows it selected instead of falling back to the first
  // entry. (Switching away rewrites the fence, as with any language.)
  const options = useMemo(() => {
    if (language && !CODE_BLOCK_LANGUAGES.some((l) => l.value === language)) {
      return [...CODE_BLOCK_LANGUAGES, { value: language, label: language }];
    }
    return CODE_BLOCK_LANGUAGES;
  }, [language]);

  const onLanguageChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      // Programmatic commands bypass the read-only DOM guard, so re-check here.
      if (!editor.isEditable) return;
      // The native <select> stole focus from the editor, so updateAttributes
      // would fire an update while blurred — which the autosave/dirty wiring
      // treats as a load-time re-baseline and never persists. Refocus first so
      // the language change saves like any edit.
      editor.commands.focus();
      updateAttributes({ language: e.target.value || null });
    },
    [editor, updateAttributes],
  );

  return (
    <NodeViewWrapper className="tiptap-code-block group relative">
      <select
        // contentEditable=false keeps ProseMirror from treating the control as
        // editable content and stealing its click/selection events.
        contentEditable={false}
        aria-label="Code block language"
        className="tiptap-code-block-lang absolute right-2 top-2 z-10 rounded border border-border bg-popover px-1.5 py-0.5 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
        value={language}
        disabled={!editable}
        onChange={onLanguageChange}
      >
        {options.map((l) => (
          <option key={l.value} value={l.value}>
            {l.label}
          </option>
        ))}
      </select>
      <pre>
        <NodeViewContent<"code"> as="code" />
      </pre>
      {isMermaid && previewSource.trim() && (
        // The diagram is read-only output; the source above stays editable.
        <div contentEditable={false} className="tiptap-mermaid-preview">
          <MermaidPreview source={previewSource} />
        </div>
      )}
    </NodeViewWrapper>
  );
}
