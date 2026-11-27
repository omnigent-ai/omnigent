// Text interactions on the review's current-version text: paints highlighted
// ranges (such as comments) into the rendered rows, and reports selections and
// clicks as character offsets into each file's current text.

import { useCallback, useEffect, useId, useMemo, useRef, type RefObject } from "react";
import type { CodeViewHandle } from "@pierre/diffs/react";
import { searchHighlightsSupported } from "@/lib/textRangeSearch";
import { pathFromReviewItemId } from "./diffReviewModel";
import { currentSideRow, readRenderedSelection, textRange } from "./renderedRows";
import type { DiffReviewContent } from "./reviewItems";
import { lineSegments, offsetAt } from "./textAnchors";

/** A range of a file's current text to highlight; active ones stand out. */
export interface ReviewTextHighlight {
  path: string;
  start: number;
  end: number;
  active: boolean;
}

/** What the person did with the review's current-version text. */
export type ReviewTextEvent =
  | {
      kind: "select";
      path: string;
      start: number;
      end: number;
      /** Viewport position of the selection's start. */
      position: { left: number; top: number } | undefined;
    }
  /** A click without a selection; `offset` is absent off the current-version text. */
  | { kind: "click"; path?: string; offset?: number }
  /** The selection went away. */
  | { kind: "clear" }
  /** The review scrolled, so anything placed beside the selection is out of place. */
  | { kind: "scroll" };

/** Current-version lines of every loaded diff, by path. */
export function currentLinesByPath(
  contents: ReadonlyMap<string, DiffReviewContent>,
): ReadonlyMap<string, readonly string[]> {
  const lines = new Map<string, readonly string[]>();
  for (const [path, content] of contents) {
    if (content.status === "ready" && !content.fileDiff.isPartial) {
      lines.set(path, content.fileDiff.additionLines);
    }
  }
  return lines;
}

function highlightNames(instanceId: string) {
  const suffix = instanceId.replace(/[^a-zA-Z0-9]/g, "");
  return {
    all: `diff-review-text-${suffix}`,
    current: `diff-review-text-active-${suffix}`,
  };
}

/** Shadow-root rules for one review's highlight names; colors come from the review's CSS variables. */
export function textHighlightCss(names: { all: string; current: string }): string {
  return `
  ::highlight(${names.all}) { background-color: var(--diff-review-comment); }
  ::highlight(${names.current}) { background-color: var(--diff-review-comment-active); }
`;
}

export function useReviewTextLayer<A>({
  viewer,
  shell,
  contents,
  highlights,
  onTextEvent,
}: {
  viewer: RefObject<CodeViewHandle<A> | null>;
  shell: HTMLElement | null;
  contents: ReadonlyMap<string, DiffReviewContent>;
  highlights: readonly ReviewTextHighlight[];
  onTextEvent?: (event: ReviewTextEvent) => void;
}) {
  const instanceId = useId();
  const names = useMemo(() => highlightNames(instanceId), [instanceId]);
  const linesByPath = useMemo(() => currentLinesByPath(contents), [contents]);
  const latest = useRef({ linesByPath, highlights, onTextEvent });
  latest.current = { linesByPath, highlights, onTextEvent };

  const renderedRoots = useCallback(() => {
    const roots = new Map<string, ShadowRoot>();
    for (const item of viewer.current?.getInstance()?.getRenderedItems() ?? []) {
      if (item.type === "diff" && item.element.shadowRoot) {
        roots.set(item.id, item.element.shadowRoot);
      }
    }
    return roots;
  }, [viewer]);

  const paint = useCallback(() => {
    if (!searchHighlightsSupported()) return;
    const { highlights: wanted, linesByPath: lines } = latest.current;
    const dim: Range[] = [];
    const active: Range[] = [];
    if (wanted.length > 0) {
      for (const [itemId, root] of renderedRoots()) {
        const path = pathFromReviewItemId(itemId);
        const fileLines = lines.get(path);
        if (!fileLines) continue;
        for (const highlight of wanted) {
          if (highlight.path !== path) continue;
          for (const segment of lineSegments(fileLines, highlight.start, highlight.end)) {
            const row = currentSideRow(root, segment.line);
            const range = row && textRange(row, segment.from, segment.to);
            if (range) (highlight.active ? active : dim).push(range);
          }
        }
      }
    }
    CSS.highlights.set(names.all, new Highlight(...dim));
    CSS.highlights.set(names.current, new Highlight(...active));
  }, [names, renderedRoots]);

  // Rows render as the reader scrolls, so painting follows every render, coalesced per frame.
  const frame = useRef<number | undefined>(undefined);
  const schedulePaint = useCallback(() => {
    if (frame.current !== undefined) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = undefined;
      paint();
    });
  }, [paint]);

  useEffect(() => schedulePaint(), [highlights, linesByPath, schedulePaint]);
  useEffect(
    () => () => {
      if (frame.current !== undefined) cancelAnimationFrame(frame.current);
      if (!searchHighlightsSupported()) return;
      CSS.highlights.delete(names.all);
      CSS.highlights.delete(names.current);
    },
    [names],
  );

  // Selections finish on mouseup or keyup; a selection that disappears hides its actions.
  const selecting = useRef(false);
  useEffect(() => {
    if (!shell) return;
    const report = () => {
      const emit = latest.current.onTextEvent;
      if (!emit) return;
      const selection = readRenderedSelection(renderedRoots());
      if (!selection) {
        selecting.current = false;
        emit({ kind: "click" });
        return;
      }
      const path = pathFromReviewItemId(selection.itemId);
      const lines = latest.current.linesByPath.get(path);
      const start = lines && offsetAt(lines, selection.start);
      const end = lines && offsetAt(lines, selection.end);
      if (start === undefined || end === undefined) {
        selecting.current = false;
        emit({ kind: "click", path });
        return;
      }
      if (selection.collapsed || start === end) {
        selecting.current = false;
        emit({ kind: "click", path, offset: start });
        return;
      }
      selecting.current = true;
      const rect = selection.anchorRect;
      emit({
        kind: "select",
        path,
        start: Math.min(start, end),
        end: Math.max(start, end),
        position: rect ? { left: rect.left, top: rect.top } : undefined,
      });
    };
    const inCode = (event: Event) =>
      event
        .composedPath()
        .some((node) => node instanceof Element && node.matches("[data-content]"));
    // Only a press that starts in the code is a text interaction; the release
    // after pressing a floating action can land on the code beneath it.
    let pressedInCode = false;
    const onPointerDown = (event: MouseEvent) => {
      pressedInCode = event.button === 0 && inCode(event);
    };
    const onPointerUp = (event: MouseEvent) => {
      if (event.button !== 0 || !pressedInCode) return;
      pressedInCode = false;
      const target = event.composedPath()[0];
      if (target instanceof Element && target.closest("button, a, input, [role=button]")) return;
      if (
        event
          .composedPath()
          .some((node) => node instanceof Element && node.matches("[data-content]"))
      ) {
        report();
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.shiftKey || event.key.startsWith("Arrow") || event.key === "Shift") report();
    };
    const onSelectionChange = () => {
      if (!selecting.current) return;
      // A selection inside a shadow root reports its ends as the host, so it
      // looks collapsed; its text is what shows whether anything is selected.
      if (document.getSelection()?.toString()) return;
      selecting.current = false;
      latest.current.onTextEvent?.({ kind: "clear" });
    };
    shell.addEventListener("mousedown", onPointerDown);
    shell.addEventListener("mouseup", onPointerUp);
    shell.addEventListener("keyup", onKeyUp);
    document.addEventListener("selectionchange", onSelectionChange);
    return () => {
      shell.removeEventListener("mousedown", onPointerDown);
      shell.removeEventListener("mouseup", onPointerUp);
      shell.removeEventListener("keyup", onKeyUp);
      document.removeEventListener("selectionchange", onSelectionChange);
    };
  }, [shell, renderedRoots]);

  const highlightCss = useMemo(() => textHighlightCss(names), [names]);

  /** Hide selection actions when the review scrolls, like other code surfaces. */
  const handleScroll = useCallback(() => {
    schedulePaint();
    if (selecting.current) latest.current.onTextEvent?.({ kind: "scroll" });
  }, [schedulePaint]);

  return {
    highlightCss,
    onPostRender: schedulePaint,
    onScroll: handleScroll,
  };
}
