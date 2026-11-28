// Node view for markdown code blocks in the rich-text editor: the fenced source
// stays editable and a <select> picks the fence language. A `mermaid` block opens
// on its read-only diagram; its Source view adds the editable source above it.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Selection } from "@tiptap/pm/state";
import { NodeViewContent, NodeViewWrapper, type ReactNodeViewProps } from "@tiptap/react";
import { cn } from "@/lib/utils";
import { MermaidPreview } from "./MermaidPreview";
import { CODE_BLOCK_LANGUAGES } from "./codeBlockLanguages";
import { CODE_BLOCK_LANGUAGE_EDIT_META } from "./codeBlockLanguageEdit";
import { revealDecorationKey } from "./TipTapRevealDecoration";

const MERMAID_VIEWS = [
  { value: "diagram", label: "Diagram" },
  { value: "source", label: "Source" },
] as const;
type MermaidView = (typeof MERMAID_VIEWS)[number]["value"];

export function TipTapCodeBlockView({
  node,
  editor,
  getPos,
  innerDecorations,
  selectionInside,
}: ReactNodeViewProps) {
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

  const [mermaidView, setMermaidView] = useState<MermaidView>("diagram");
  // A block that just gained diagram source (undo, a remote edit, a language
  // change) keeps it shown until the diagram draws, so it never goes blank.
  const hasDiagramSource = isMermaid && source.trim() !== "";
  const [hadDiagramSource, setHadDiagramSource] = useState(hasDiagramSource);
  const [awaitingFirstDraw, setAwaitingFirstDraw] = useState(false);
  if (hadDiagramSource !== hasDiagramSource) {
    setHadDiagramSource(hasDiagramSource);
    setAwaitingFirstDraw(hasDiagramSource);
  }
  const onDiagramDrawn = useCallback(() => setAwaitingFirstDraw(false), []);
  const canShowDiagram = hasDiagramSource && previewSource.trim() !== "";
  // Choosing Diagram hides the current find match or active comment until it moves.
  const revealKey = revealDecorationKey(innerDecorations);
  const [dismissedRevealKey, setDismissedRevealKey] = useState("");
  if (dismissedRevealKey && dismissedRevealKey !== revealKey) setDismissedRevealKey("");
  // Keep the source visible while the caret, the current find match, or the
  // active comment is in it, so nobody edits or navigates to hidden text.
  const showSource =
    !canShowDiagram ||
    awaitingFirstDraw ||
    mermaidView === "source" ||
    selectionInside === true ||
    (revealKey !== "" && revealKey !== dismissedRevealKey);

  const selectMermaidView = useCallback(
    (view: MermaidView) => {
      setMermaidView(view);
      if (view !== "diagram") return;
      setDismissedRevealKey(revealKey);
      if (!selectionInside) return;
      // The caret would keep the source open; park it beside the block instead.
      const pos = getPos();
      if (typeof pos !== "number") return;
      const { state } = editor.view;
      const target =
        Selection.findFrom(state.doc.resolve(pos + node.nodeSize), 1, true) ??
        Selection.findFrom(state.doc.resolve(pos), -1, true);
      if (target) editor.view.dispatch(state.tr.setSelection(target));
    },
    [editor, getPos, node.nodeSize, revealKey, selectionInside],
  );

  // The editor is read-only for a file the user can't edit; the language
  // picker must not offer to mutate the doc in that case.
  const editable = editor.isEditable;
  // Hundreds of unopened native pickers otherwise create thousands of option
  // elements. Keep the selected label, then populate the menu before use.
  const [optionsReady, setOptionsReady] = useState(false);

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

  const languagePicker = (
    <select
      // contentEditable=false keeps ProseMirror from treating the control as
      // editable content and stealing its click/selection events.
      contentEditable={false}
      aria-label="Code block language"
      className={cn(
        "tiptap-code-block-lang rounded border border-border bg-popover px-1.5 py-0.5 text-xs text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100",
        !isMermaid && "absolute right-2 top-2 z-10",
      )}
      value={selectValue}
      disabled={!editable}
      onPointerEnter={() => setOptionsReady(true)}
      onPointerDown={() => setOptionsReady(true)}
      onFocus={() => setOptionsReady(true)}
      onChange={onLanguageChange}
    >
      {(optionsReady ? options : options.filter((l) => l.value === selectValue)).map((l) => (
        <option key={l.value} value={l.value}>
          {l.label}
        </option>
      ))}
    </select>
  );

  return (
    <NodeViewWrapper className="tiptap-code-block group relative">
      {isMermaid ? (
        // The diagram card draws its own header, so these controls get a row
        // above the block instead of floating over it.
        <div
          contentEditable={false}
          className="tiptap-code-block-toolbar mb-1 flex items-center justify-end gap-1.5"
        >
          {canShowDiagram && (
            <div
              role="group"
              aria-label="Mermaid view"
              className="inline-flex rounded border border-border bg-popover p-px text-xs"
            >
              {MERMAID_VIEWS.map(({ value, label }) => {
                const active = value === (showSource ? "source" : "diagram");
                return (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={active}
                    className={cn(
                      "rounded-sm px-1.5 py-0.5 transition-colors",
                      active
                        ? "bg-muted text-foreground"
                        : "text-muted-foreground hover:text-foreground",
                    )}
                    onClick={() => selectMermaidView(value)}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          )}
          {languagePicker}
        </div>
      ) : (
        languagePicker
      )}
      <pre hidden={!showSource}>
        <NodeViewContent<"code"> as="code" />
      </pre>
      {canShowDiagram && (
        // The diagram stays under the source as a live preview while editing.
        <div contentEditable={false} className="tiptap-mermaid-preview">
          <MermaidPreview source={previewSource} onDrawn={onDiagramDrawn} />
        </div>
      )}
    </NodeViewWrapper>
  );
}
