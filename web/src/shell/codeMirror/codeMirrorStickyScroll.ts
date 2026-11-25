// Sticky scroll, as in Monaco: the lines that open the indentation scopes around the top of the
// view stay pinned above the text, and clicking one scrolls back to it.

import type { Extension, Text } from "@codemirror/state";
import {
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type PluginValue,
  type ViewUpdate,
} from "@codemirror/view";
import {
  CLOSER_SCAN_LINES,
  IndentationScopes,
  MAX_SHARE_OF_VIEW,
  NO_STICKY_LINES,
  sharedScopeFiller,
  STICKY_SCROLL_MAX_LINES,
  type StickyScrollGeometry,
  type StickyScrollLines,
  stickyScrollLines,
} from "@/lib/indentationScopes";
import { SHIKI_TOKEN_CLASS } from "./shikiDecorations";
import { highlightingOf } from "./viewportHighlighting";

/**
 * Appends `text`, which starts at document position `from`, to `parent`, with the syntax colors
 * `highlighting` gives it.
 */
export function appendHighlightedText(
  parent: HTMLElement,
  text: string,
  from: number,
  highlighting: DecorationSet,
): void {
  const to = from + text.length;
  let at = from;
  highlighting.between(from, to, (start, end, decoration) => {
    const tokenFrom = Math.max(start, from);
    const tokenTo = Math.min(end, to);
    if (tokenFrom >= tokenTo) return;
    if (tokenFrom > at) parent.append(text.slice(at - from, tokenFrom - from));
    const token = document.createElement("span");
    token.className = SHIKI_TOKEN_CLASS;
    const style = (decoration.spec as { attributes?: { style?: string } }).attributes?.style;
    if (style) token.setAttribute("style", style);
    token.textContent = text.slice(tokenFrom - from, tokenTo - from);
    parent.append(token);
    at = tokenTo;
  });
  if (at < to) parent.append(text.slice(at - from));
}

/** Appends `line`'s text to `parent`, with the syntax colors the viewer has for it. */
function renderLineText(parent: HTMLElement, doc: Text, highlighting: DecorationSet, line: number) {
  const { from, text } = doc.line(line);
  appendHighlightedText(parent, text, from, highlighting);
}

interface Measured {
  /** Null while the links don't reach the view yet. */
  sticky: StickyScrollLines | null;
  scrollLeft: number;
  /** The scroller's top padding, which a sticky child would otherwise hang below. */
  paddingTop: number;
  width: number;
  lineHeight: number;
  gutterWidth: number;
  numberWidth: number;
  numberPaddingRight: string;
  textLeft: number;
}

class StickyScrollView implements PluginValue {
  private readonly root: HTMLElement;
  private readonly rows: HTMLElement;
  private readonly scopes: IndentationScopes;
  private shown: { lines: number[]; doc: Text | null; highlighting: DecorationSet | null } = {
    lines: [],
    doc: null,
    highlighting: null,
  };
  private readonly onScroll = () => this.schedule();
  private readonly onFilled = () => this.schedule();
  private destroyed = false;
  /** Set by an edit until the links catch up, so the rows stay up instead of blinking off. */
  private editedSinceFilled = false;
  private readonly view: EditorView;

  constructor(view: EditorView) {
    this.view = view;
    this.scopes = new IndentationScopes(view.state.doc, view.state.tabSize);
    // Pinned to the scroller's top-left corner, above the gutters, below the find bar.
    this.root = document.createElement("div");
    this.root.className = "cm-stickyScroll";
    this.root.setAttribute("aria-hidden", "true");
    this.rows = document.createElement("div");
    this.rows.className = "cm-stickyScroll-rows";
    this.root.append(this.rows);
    this.root.addEventListener("mousedown", this.onMouseDown);
    view.scrollDOM.prepend(this.root);
    view.scrollDOM.addEventListener("scroll", this.onScroll, { passive: true });
    this.schedule();
  }

  update(update: ViewUpdate): void {
    if (update.docChanged || update.state.tabSize !== update.startState.tabSize) {
      this.editedSinceFilled = true;
      this.scopes.update(
        update.state.doc,
        update.state.tabSize,
        update.docChanged ? update.changes : null,
      );
    }
    if (
      update.docChanged ||
      update.geometryChanged ||
      update.viewportChanged ||
      update.heightChanged ||
      highlightingOf(update.state) !== highlightingOf(update.startState)
    ) {
      this.schedule();
    }
  }

  destroy(): void {
    this.destroyed = true;
    sharedScopeFiller.cancel(this.scopes, this.onFilled);
    this.view.scrollDOM.removeEventListener("scroll", this.onScroll);
    this.root.remove();
  }

  private schedule(): void {
    if (this.destroyed) return;
    this.view.requestMeasure({
      key: this,
      read: (view) => this.measure(view),
      write: (measured) => this.draw(measured),
    });
  }

  private measure(view: EditorView): Measured {
    const scroller = view.scrollDOM;
    const scrollerRect = scroller.getBoundingClientRect();
    const lineHeight = view.defaultLineHeight;
    const visibleLines = Math.floor(scroller.clientHeight / lineHeight);
    const geometry: StickyScrollGeometry = {
      top: scrollerRect.top - view.documentTop,
      lineHeight,
      maxLines: Math.min(STICKY_SCROLL_MAX_LINES, Math.floor(visibleLines * MAX_SHARE_OF_VIEW)),
      lineCount: view.state.doc.lines,
      lineAtHeight: (height) => view.state.doc.lineAt(view.lineBlockAtHeight(height).from).number,
      lineBottom: (lineNumber) => view.lineBlockAt(view.state.doc.line(lineNumber).from).bottom,
    };
    const gutters = view.dom.querySelector<HTMLElement>(".cm-gutters");
    const numbers = view.dom.querySelector<HTMLElement>(".cm-lineNumbers");
    // Measuring only reads links; until they reach the view, the shared filler fills them.
    const lastNeeded =
      geometry.lineAtHeight(geometry.top + geometry.maxLines * lineHeight) + CLOSER_SCAN_LINES;
    const ready = this.scopes.reaches(lastNeeded);
    if (!ready) sharedScopeFiller.request(this.scopes, lastNeeded, this.onFilled);
    const numberElement = numbers?.querySelector<HTMLElement>(
      ".cm-gutterElement:not([style*='visibility'])",
    );
    const contentStyle = getComputedStyle(view.contentDOM);
    return {
      sticky: ready ? stickyScrollLines(this.scopes, geometry) : null,
      scrollLeft: scroller.scrollLeft,
      paddingTop: Number.parseFloat(getComputedStyle(scroller).paddingTop) || 0,
      width: scroller.clientWidth,
      lineHeight,
      gutterWidth: gutters?.offsetWidth ?? 0,
      numberWidth: numbers ? numbers.offsetLeft + numbers.offsetWidth : 0,
      numberPaddingRight: numberElement ? getComputedStyle(numberElement).paddingRight : "0px",
      textLeft:
        view.contentDOM.getBoundingClientRect().left -
        scrollerRect.left +
        scroller.scrollLeft +
        (Number.parseFloat(contentStyle.paddingLeft) || 0),
    };
  }

  private draw(measured: Measured): void {
    // Still filling: after an edit the rows stay as they are; after a jump they clear.
    if (measured.sticky === null && this.editedSinceFilled) return;
    if (measured.sticky !== null) this.editedSinceFilled = false;
    const { lineHeight } = measured;
    const sticky = measured.sticky ?? NO_STICKY_LINES;
    const { doc } = this.view.state;
    const highlighting = highlightingOf(this.view.state);
    const unchanged =
      this.shown.doc === doc &&
      this.shown.highlighting === highlighting &&
      this.shown.lines.length === sticky.lines.length &&
      this.shown.lines.every((line, index) => line === sticky.lines[index]);
    if (!unchanged) {
      this.rows.replaceChildren(
        ...sticky.lines.map((line, depth) => this.renderRow(line, depth, highlighting)),
      );
      this.shown = { lines: sticky.lines, doc, highlighting };
    }
    this.root.classList.toggle("cm-stickyScroll-shown", sticky.lines.length > 0);
    const height = sticky.lines.length * lineHeight - sticky.pushUp;
    // Flush with the scroller's top edge: sticky offsets count from inside its padding.
    this.root.style.top = `${-measured.paddingTop}px`;
    this.rows.style.width = `${measured.width}px`;
    this.rows.style.height = `${Math.max(height, 0)}px`;
    this.rows.style.setProperty("--cm-sticky-line-height", `${lineHeight}px`);
    this.rows.style.setProperty("--cm-sticky-gutter-width", `${measured.gutterWidth}px`);
    this.rows.style.setProperty("--cm-sticky-number-width", `${measured.numberWidth}px`);
    this.rows.style.setProperty("--cm-sticky-number-padding", measured.numberPaddingRight);
    this.rows.style.setProperty(
      "--cm-sticky-text-offset",
      `${measured.textLeft - measured.gutterWidth - measured.scrollLeft}px`,
    );
    this.rows.style.tabSize = String(this.view.state.tabSize);
    const rows = this.rows.children;
    for (let depth = 0; depth < rows.length; depth += 1) {
      const row = rows[depth] as HTMLElement;
      const innermost = depth === rows.length - 1;
      row.style.top = `${depth * lineHeight - (innermost ? sticky.pushUp : 0)}px`;
    }
  }

  private renderRow(line: number, depth: number, highlighting: DecorationSet): HTMLElement {
    const row = document.createElement("div");
    row.className = STICKY_LINE_CLASS;
    row.dataset.line = String(line);
    row.dataset.depth = String(depth);
    const number = document.createElement("span");
    number.className = "cm-stickyScroll-number";
    number.textContent = String(line);
    const text = document.createElement("span");
    text.className = "cm-stickyScroll-text";
    const code = document.createElement("span");
    code.className = "cm-stickyScroll-code";
    renderLineText(code, this.view.state.doc, highlighting, line);
    text.append(code);
    row.append(number, text);
    return row;
  }

  private readonly onMouseDown = (event: MouseEvent) => {
    if (event.button !== 0) return;
    const pinned = pinnedLineAt(this.view, event.target);
    if (!pinned) return;
    // Keep the editor's own selection handling out of it; this click navigates.
    event.preventDefault();
    event.stopPropagation();
    revealStickyLine(this.view, pinned.line, pinned.depth);
  };
}

/** The class of a pinned row, which carries its line number and depth as data attributes. */
export const STICKY_LINE_CLASS = "cm-stickyScroll-line";

/** The pinned line, and its depth, that an event in `view` landed on; null off the pinned rows. */
export function pinnedLineAt(
  view: EditorView,
  target: EventTarget | null,
): { line: number; depth: number } | null {
  if (!(target instanceof Element)) return null;
  const row = target.closest<HTMLElement>(`.${STICKY_LINE_CLASS}`);
  if (!row || !view.scrollDOM.contains(row)) return null;
  const line = Number(row.dataset.line);
  const depth = Number(row.dataset.depth);
  return Number.isInteger(line) && Number.isInteger(depth) ? { line, depth } : null;
}

/**
 * Moves the cursor to a pinned line and scrolls it to where it was pinned, under the scopes
 * still pinned above it, as Monaco does.
 */
export function revealStickyLine(view: EditorView, lineNumber: number, depth: number): void {
  if (lineNumber < 1 || lineNumber > view.state.doc.lines) return;
  const line = view.state.doc.line(lineNumber);
  view.dispatch({
    selection: { anchor: line.from },
    effects: EditorView.scrollIntoView(line.from, {
      y: "start",
      yMargin: depth * view.defaultLineHeight,
    }),
    userEvent: "select.pointer",
  });
  view.focus();
}

/**
 * Pins the lines that open the indentation scopes around the top of the view. One shared
 * instance, so reconfiguring with the same choice keeps the pinned rows.
 */
export const stickyScroll: Extension = ViewPlugin.define((view) => new StickyScrollView(view));
