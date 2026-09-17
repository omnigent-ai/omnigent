import { useEffect, type RefObject } from "react";
import type { DiffOnMount } from "@monaco-editor/react";
import type { FilePosition } from "./FileViewerContext";
import type { CodeEditorInstance } from "./useMonacoCommentLayer";

/** Center a citation after layout/diff calculation, until the reader takes over. */
export function useMonacoFilePosition({
  editorRef,
  mounted,
  position,
  cancelScrollRestoreRef,
  diffEditorRef,
}: {
  editorRef: RefObject<CodeEditorInstance | null>;
  mounted: boolean;
  position?: FilePosition;
  cancelScrollRestoreRef: RefObject<(() => void) | null>;
  diffEditorRef?: RefObject<Parameters<DiffOnMount>[0] | null>;
}) {
  useEffect(() => {
    const editor = editorRef.current;
    if (!mounted || !editor || !position) return;
    cancelScrollRestoreRef.current?.();
    const diff = diffEditorRef?.current;
    const center = () => {
      const model = editor.getModel();
      const { width, height } = editor.getLayoutInfo();
      if (!model || width <= 0 || height <= 0 || (diff && diff.getLineChanges() === null)) return;
      const target = model.validatePosition({
        lineNumber: position.line,
        column: position.column ?? 1,
      });
      // Moving the cursor expands collapsed diff context containing the target.
      editor.setPosition(target);
      editor.revealPositionInCenter(target, 1 /* ScrollType.Immediate */);
    };
    const layout = editor.onDidLayoutChange(center);
    let frame: number | undefined;
    const diffUpdate = diff?.onDidUpdateDiff(() => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(center);
    });
    const dom = diff?.getContainerDomNode() ?? editor.getDomNode();
    const events = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    const stop = () => {
      layout.dispose();
      diffUpdate?.dispose();
      if (frame !== undefined) cancelAnimationFrame(frame);
      for (const event of events) dom?.removeEventListener(event, stop, { capture: true });
    };
    // Capture interaction before Monaco consumes events in its child editors.
    for (const event of events)
      dom?.addEventListener(event, stop, { passive: true, capture: true });
    center();
    return stop;
  }, [editorRef, mounted, position, cancelScrollRestoreRef, diffEditorRef]);
}
