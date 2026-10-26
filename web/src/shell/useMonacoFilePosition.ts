import { useEffect, type RefObject } from "react";
import type { DiffOnMount } from "@monaco-editor/react";
import type { FilePosition } from "./FileViewerContext";
import { isFilePositionPending, stopFilePosition } from "./filePositionState";
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
    if (!mounted || !editor || !position || !isFilePositionPending(position)) return;
    cancelScrollRestoreRef.current?.();
    const diff = diffEditorRef?.current;
    const center = () => {
      if (!isFilePositionPending(position)) return;
      const model = editor.getModel();
      const { width, height } = editor.getLayoutInfo();
      if (!model || width <= 0 || height <= 0 || (diff && diff.getLineChanges() === null)) return;
      const target = model.validatePosition({
        lineNumber: position.line,
        column: position.column ?? 1,
      });
      // Moving the cursor expands collapsed diff context containing the target.
      editor.setPosition(target);
      if (position.endLine) {
        const endLine = model.validatePosition({ lineNumber: position.endLine, column: 1 });
        const rangeHeight =
          editor.getBottomForLineNumber(endLine.lineNumber) -
          editor.getTopForLineNumber(target.lineNumber);
        // A range taller than the viewport is revealed from its first line.
        if (rangeHeight <= height)
          editor.revealRangeInCenter(
            citedRange(target.lineNumber, endLine.lineNumber),
            1 /* ScrollType.Immediate */,
          );
        else editor.revealLineNearTop(target.lineNumber, 1 /* ScrollType.Immediate */);
      } else {
        editor.revealPositionInCenter(target, 1 /* ScrollType.Immediate */);
      }
    };
    const layout = editor.onDidLayoutChange(center);
    let frame: number | undefined;
    const diffUpdate = diff?.onDidUpdateDiff(() => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(center);
    });
    const dom = diff?.getContainerDomNode() ?? editor.getDomNode();
    const events = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    const stop = (interaction?: Event) => {
      if (interaction) stopFilePosition(position);
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

  // A cited range stays highlighted while it is the viewer's position, even
  // after the reader scrolls away from it.
  useEffect(() => {
    const editor = editorRef.current;
    if (!mounted || !editor || !position?.endLine) return;
    const decorations = [
      {
        range: citedRange(position.line, position.endLine),
        options: { isWholeLine: true, className: CITED_LINES_CLASS_NAME },
      },
    ];
    const highlight = editor.createDecorationsCollection(decorations);
    // Replacing the whole value (an external file reload) drops model decorations.
    const reload = editor.onDidChangeModelContent((event) => {
      if (event.isFlush) highlight.set(decorations);
    });
    return () => {
      reload.dispose();
      highlight.clear();
    };
  }, [editorRef, mounted, position]);
}

const CITED_LINES_CLASS_NAME = "oa-cited-lines";

function citedRange(startLine: number, endLine: number) {
  return { startLineNumber: startLine, startColumn: 1, endLineNumber: endLine, endColumn: 1 };
}
