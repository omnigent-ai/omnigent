// Node view for markdown code blocks in the rich-text editor: the fenced source
// stays editable, a <select> picks the fence language, and a `mermaid` block
// renders a live read-only diagram below the source.

import { useCallback, useEffect, useMemo, useState } from "react";
import { NodeViewWrapper, useReactNodeView, type NodeViewProps } from "@tiptap/react";
import { MermaidPreview } from "./MermaidPreview";
import { CODE_BLOCK_LANGUAGES } from "./codeBlockLanguages";
import { CODE_BLOCK_LANGUAGE_EDIT_META } from "./codeBlockLanguageEdit";

/**
 * The editable fence body, wired like @tiptap/react's NodeViewContent: the
 * editor's content element waits beside the React tree until it is moved in
 * here. Upstream first reads the DOM selection so it can put a caret back after
 * the move, and that read forces the browser to lay out the whole document —
 * once per code block while a large file opens. A caret only needs preserving
 * while the editor has focus, so an unfocused editor moves the content directly.
 * The direct move mirrors the ref `ReactNodeView.mount` wires in @tiptap/react
 * 3.30.5 (the content element waits as the `.react-renderer` host's
 * `[data-node-view-content-react]` child); re-check it on upgrades.
 */
function CodeBlockContent({ editor }: { editor: NodeViewProps["editor"] }) {
  const { nodeViewContentRef } = useReactNodeView();
  const ref = useCallback(
    (element: HTMLElement | null) => {
      if (!element || editor.isFocused) {
        nodeViewContentRef?.(element);
        return;
      }
      const content = element
        .closest(".react-renderer")
        ?.querySelector(":scope > [data-node-view-content-react]");
      if (!content) {
        // The host's markup is not where this version of @tiptap/react put it;
        // let upstream place the content rather than leave the fence empty.
        nodeViewContentRef?.(element);
        return;
      }
      if (element.firstChild === content) return;
      content.removeAttribute("data-node-view-wrapper");
      element.appendChild(content);
    },
    [editor, nodeViewContentRef],
  );
  return <code ref={ref} data-node-view-content="" className="whitespace-pre-wrap" />;
}

export function TipTapCodeBlockView({ node, editor, getPos }: NodeViewProps) {
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

  // Match a known language case-insensitively so a cased fence (```Mermaid) maps
  // to the canonical option; a truly unknown language (e.g. ```rust) is appended
  // so the picker still shows it selected instead of the first entry.
  const { options, selectValue } = useMemo(() => {
    const known = CODE_BLOCK_LANGUAGES.find(
      (l) => l.value.toLowerCase() === language.toLowerCase(),
    );
    if (language && !known) {
      return {
        options: [...CODE_BLOCK_LANGUAGES, { value: language, label: language }],
        selectValue: language,
      };
    }
    return { options: CODE_BLOCK_LANGUAGES, selectValue: known?.value ?? language };
  }, [language]);

  const onLanguageChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      // Programmatic commands bypass the read-only DOM guard, so re-check here.
      if (!editor.isEditable) return;
      const pos = getPos();
      if (typeof pos !== "number") return;
      // The native <select> holds focus, so this update reaches the editor while
      // it is blurred. Flag the transaction as a user edit so autosave persists
      // it — refocusing programmatically is unreliable (TipTap defers focus).
      const tr = editor.view.state.tr
        .setNodeAttribute(pos, "language", e.target.value || null)
        .setMeta(CODE_BLOCK_LANGUAGE_EDIT_META, true);
      editor.view.dispatch(tr);
    },
    [editor, getPos],
  );

  return (
    <NodeViewWrapper className="tiptap-code-block group relative">
      <select
        // contentEditable=false keeps ProseMirror from treating the control as
        // editable content and stealing its click/selection events.
        contentEditable={false}
        aria-label="Code block language"
        className="tiptap-code-block-lang absolute right-2 top-2 z-10 rounded border border-border bg-popover px-1.5 py-0.5 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
        value={selectValue}
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
        <CodeBlockContent editor={editor} />
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
