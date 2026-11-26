// Maps the rendered Markdown preview back to source offsets so comments made on
// the preview anchor to the same characters as comments made in Source.
//
// A rehype plugin wraps every positioned text node in a span carrying its source
// range. A rendered run usually equals its source slice (plain prose); when it
// doesn't (entities, escapes, inline code with backticks) the run is located
// inside its slice, and failing that its edges snap to the slice's edges.

const START_ATTRIBUTE = "data-md-start";
const END_ATTRIBUTE = "data-md-end";
const SPAN_SELECTOR = `span[${START_ATTRIBUTE}]`;
// Subtrees whose text isn't readable prose, or where a span would be invalid.
const SKIPPED_TAGS = new Set(["svg", "math", "style", "script", "annotation"]);

interface HastPoint {
  offset?: number;
}
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  position?: { start: HastPoint; end: HastPoint };
}

function wrapTextNodes(node: HastNode): void {
  if (!node.children) return;
  if (node.type === "element" && node.tagName && SKIPPED_TAGS.has(node.tagName)) return;
  node.children = node.children.map((child) => {
    const start = child.position?.start.offset;
    const end = child.position?.end.offset;
    if (child.type === "text" && start != null && end != null && child.value?.trim()) {
      return {
        type: "element",
        tagName: "span",
        properties: { dataMdStart: start, dataMdEnd: end },
        children: [child],
      };
    }
    wrapTextNodes(child);
    return child;
  });
}

/** rehype plugin: tag each source-positioned text run with its source range. */
export function rehypeSourceOffsets() {
  return (tree: HastNode) => {
    wrapTextNodes(tree);
  };
}

export interface SourceRun {
  /** The span's only text node. */
  text: Text;
  /** Source offset of the rendered text's first character, when it maps linearly. */
  linearStart: number | null;
  sliceStart: number;
  sliceEnd: number;
}

function readRun(span: Element, source: string): SourceRun | null {
  const text = span.firstChild;
  if (!(text instanceof Text)) return null;
  const sliceStart = Number(span.getAttribute(START_ATTRIBUTE));
  const sliceEnd = Number(span.getAttribute(END_ATTRIBUTE));
  if (!Number.isFinite(sliceStart) || !Number.isFinite(sliceEnd)) return null;
  const rendered = text.data;
  let linearStart: number | null = null;
  if (source.slice(sliceStart, sliceStart + rendered.length) === rendered) {
    linearStart = sliceStart;
  } else {
    const found = source.slice(sliceStart, sliceEnd).indexOf(rendered);
    if (found >= 0) linearStart = sliceStart + found;
  }
  return { text, linearStart, sliceStart, sliceEnd };
}

function toSource(run: SourceRun, localOffset: number, edge: "start" | "end"): number {
  if (run.linearStart !== null) return run.linearStart + localOffset;
  return edge === "start" ? run.sliceStart : run.sliceEnd;
}

/** Every mappable text run in the preview, in document order. */
export function previewRuns(root: Element, source: string): SourceRun[] {
  return Array.from(root.querySelectorAll(SPAN_SELECTOR))
    .map((span) => readRun(span, source))
    .filter((run): run is SourceRun => run !== null);
}

export interface SourceSelection {
  start_index: number;
  end_index: number;
  anchor_content: string;
}

/**
 * The source range a preview selection covers, or null when it touches no
 * mappable text (for example only a rendered formula or an image).
 */
export function previewSelectionToSource(
  range: Range,
  root: Element,
  source: string,
): SourceSelection | null {
  const runs = previewRuns(root, source).filter((run) => range.intersectsNode(run.text));
  if (runs.length === 0) return null;
  const first = runs[0];
  const last = runs[runs.length - 1];
  const startLocal = range.startContainer === first.text ? range.startOffset : 0;
  const endLocal = range.endContainer === last.text ? range.endOffset : last.text.data.length;
  const start = toSource(first, startLocal, "start");
  const end = toSource(last, endLocal, "end");
  if (end <= start) return null;
  return { start_index: start, end_index: end, anchor_content: source.slice(start, end) };
}

/** The source offset under a collapsed caret, or null outside mappable text. */
export function previewCaretToSource(
  node: Node,
  offset: number,
  root: Element,
  source: string,
): number | null {
  const span = (node instanceof Element ? node : node.parentElement)?.closest(SPAN_SELECTOR);
  if (!span || !root.contains(span)) return null;
  const run = readRun(span, source);
  if (!run) return null;
  return toSource(run, node === run.text ? offset : 0, "start");
}

/**
 * DOM ranges covering the rendered text of a source range, in document order.
 * Pass the runs from one `previewRuns` call when painting several ranges.
 */
export function sourceRangeToPreviewRanges(
  runs: readonly SourceRun[],
  start: number,
  end: number,
): Range[] {
  const ranges: Range[] = [];
  for (const run of runs) {
    if (run.sliceEnd <= start || run.sliceStart >= end) continue;
    const length = run.text.data.length;
    let localStart = 0;
    let localEnd = length;
    if (run.linearStart !== null) {
      localStart = Math.max(0, start - run.linearStart);
      localEnd = Math.min(length, end - run.linearStart);
    }
    if (localEnd <= localStart) continue;
    const range = document.createRange();
    range.setStart(run.text, localStart);
    range.setEnd(run.text, localEnd);
    ranges.push(range);
  }
  return ranges;
}
