import { useLayoutEffect, useRef, useState, type RefObject } from "react";

/**
 * Slanted italics for the composer's highlight layer, without moving a glyph.
 *
 * A real italic face has other advance widths, so in-flow italic text stays in
 * the regular face. For each italic span near the viewport, a skewed copy of
 * each of its line fragments is drawn at the measured glyph positions, in an
 * absolutely positioned overlay inside the layer (so it scrolls with it), and
 * the in-flow span turns transparent (`data-slanted`). The copies' text comes
 * from `::before { content: attr(data-text) }`, so it isn't in the layer's
 * text. A span keeps the dotted underline whenever it gets no copy: when the
 * layer can't be measured (hidden, not laid out, or a DOM without layout), the
 * span holds a tab (tab stops wouldn't match) or more than one text node, its
 * line holds right-to-left text or a bidi control (reordering moves glyphs off
 * a left-to-right copy), or for a frame after it scrolls into view beyond the
 * measured margin.
 */

// Right-to-left scripts (Hebrew, Arabic, Syriac, Thaana, N'Ko and neighbors,
// their presentation forms, astral RTL ranges) and bidi controls.
const BIDI_RE =
  /[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff\u200e\u200f\u061c\u202a-\u202e\u2066-\u2069\u{10800}-\u{10fff}\u{1e800}-\u{1efff}]/u;

/** Spans this far above and below the viewport (in viewports) get copies too. */
const MARGIN_VIEWPORTS = 1;
/** The span window moves once the viewport comes this close (in viewports) to its edge. */
const MIN_MARGIN_VIEWPORTS = 0.5;

interface SlantEntry {
  /** What the span's copies were built from; a changed span rebuilds its own. */
  key: unknown[];
  /** Its copies in the overlay: none when it keeps the dotted underline. */
  copies: HTMLElement[];
}

export interface SlantState {
  /** The nearby italic spans and their copies. */
  spans: Map<HTMLElement, SlantEntry>;
  /** The layer width and font every copy was built for; a change rebuilds all. */
  frame: unknown[];
  /** The stretch of the layer (content pixels) whose spans get copies. */
  window: { top: number; bottom: number; height: number } | null;
  /** The copy text's offset below its box top, per layer font. */
  lift: { font: string; value: number } | null;
}

export const newSlantState = (): SlantState => ({
  spans: new Map(),
  frame: [],
  window: null,
  lift: null,
});

/**
 * Bring the overlay's copies up to date for the italic spans near the layer's
 * viewport. Only spans that changed, moved, or came near get new copies, so a
 * keystroke or a line of scrolling touches a row's worth, not the viewport's.
 */
export function slantItalics(layer: HTMLElement, overlay: HTMLElement, state: SlantState) {
  const spans =
    layer.clientHeight > 0 ? nearbyItalics(layer, overlay, windowFor(layer, state)) : [];
  if (spans.length === 0) {
    clearSlant(overlay, state);
    return;
  }
  const style = getComputedStyle(layer);
  const font = `${style.font}|${style.lineHeight}|${style.letterSpacing}`;
  // The fractional width: a fraction of a pixel can move a wrap.
  const box = layer.getBoundingClientRect();
  const frame = [box.width, layer.clientWidth, font];
  if (!sameList(frame, state.frame)) {
    clearSlant(overlay, state);
    state.frame = frame;
  }
  // Measured once, and only if some span needs new copies.
  let lift: number | null | undefined;
  const liftNow = () => {
    if (lift === undefined) lift = box.width > 0 ? liftFor(overlay, state, font) : null;
    return lift;
  };
  const left = layer.scrollLeft - box.left;
  const top = layer.scrollTop - box.top;
  const rtlRows = new Map<Element, boolean>();
  const next = new Map<HTMLElement, SlantEntry>();
  // Walk back from the last span so new copies go in before the next span's:
  // the overlay keeps its copies in draft order.
  let following: HTMLElement | null = null;
  for (let i = spans.length - 1; i >= 0; i--) {
    const span = spans[i];
    const row = span.closest("div") ?? span;
    let rtl = rtlRows.get(row);
    if (rtl === undefined) {
      rtl = BIDI_RE.test(row.textContent ?? "");
      rtlRows.set(row, rtl);
    }
    const text = span.firstChild;
    const key: unknown[] = [text, text instanceof Text ? text.data : null, span.className, rtl];
    // Where each of its line fragments sits in the layer, to the fraction.
    for (const rect of span.getClientRects())
      key.push(rect.left + left, rect.top + top, rect.width);
    let entry = state.spans.get(span);
    state.spans.delete(span);
    if (!entry || !sameList(entry.key, key)) {
      if (entry) dropCopies(span, entry);
      const at = liftNow();
      if (at === null) {
        // Not measurable: every span goes back to the dotted underline.
        for (const [done, kept] of next) state.spans.set(done, kept);
        clearSlant(overlay, state);
        return;
      }
      entry = { key, copies: rtl ? [] : copiesFor(span, layer, box, at) };
      for (const copy of entry.copies) overlay.insertBefore(copy, following);
      if (entry.copies.length > 0) span.setAttribute("data-slanted", "");
    }
    next.set(span, entry);
    if (entry.copies.length > 0) following = entry.copies[0];
  }
  // Spans that left the window, or the layer, lose their copies.
  for (const [span, entry] of state.spans) dropCopies(span, entry);
  state.spans = next;
}

/** One span's skewed copies, one per line fragment; none if it can't be copied exactly. */
function copiesFor(span: HTMLElement, layer: HTMLElement, box: DOMRect, lift: number) {
  const text = span.firstChild;
  if (!(text instanceof Text) || span.childNodes.length !== 1 || text.data.includes("\t"))
    return [];
  const className = span.className
    .split(/\s+/)
    .filter((name) => name !== "" && name !== "md-em")
    .concat("md-slant")
    .join(" ");
  return lineFragments(text).map((fragment) => {
    const copy = document.createElement("span");
    copy.className = className;
    copy.dataset.text = fragment.text;
    copy.style.left = `${fragment.left - box.left + layer.scrollLeft}px`;
    copy.style.top = `${fragment.top - box.top + layer.scrollTop - lift}px`;
    return copy;
  });
}

function dropCopies(span: HTMLElement, entry: SlantEntry) {
  for (const copy of entry.copies) copy.remove();
  if (entry.copies.length > 0) span.removeAttribute("data-slanted");
}

/** Remove every copy: each span goes back to the dotted underline. */
function clearSlant(overlay: HTMLElement, state: SlantState) {
  for (const [span, entry] of state.spans)
    if (entry.copies.length > 0) span.removeAttribute("data-slanted");
  state.spans = new Map();
  overlay.replaceChildren();
}

const sameList = (a: unknown[], b: unknown[]) =>
  a.length === b.length && a.every((value, i) => value === b[i]);

/**
 * The window of spans to copy: the viewport plus a margin each side. It holds
 * while the viewport stays at least half a viewport from its edges, so
 * scrolling line by line (as arrow keys do) doesn't shift it each time.
 */
function windowFor(layer: HTMLElement, state: SlantState) {
  const height = layer.clientHeight;
  const top = layer.scrollTop;
  const current = state.window;
  if (
    current &&
    current.height === height &&
    current.top <= top - height * MIN_MARGIN_VIEWPORTS &&
    current.bottom >= top + height * (1 + MIN_MARGIN_VIEWPORTS)
  )
    return current;
  state.window = {
    top: top - height * MARGIN_VIEWPORTS,
    bottom: top + height * (1 + MARGIN_VIEWPORTS),
    height,
  };
  return state.window;
}

/**
 * The italic spans within a window of the layer. The layer's children are
 * chunks of lines in draft order, so a binary search by offset finds the ones
 * in it without touching the rest of a long draft.
 */
function nearbyItalics(
  layer: HTMLElement,
  overlay: HTMLElement,
  { top, bottom }: { top: number; bottom: number },
): HTMLElement[] {
  const items = layer.children;
  let count = items.length;
  if (count > 0 && items[count - 1] === overlay) count--;
  let low = 0;
  let high = count;
  while (low < high) {
    const mid = (low + high) >> 1;
    const item = items[mid] as HTMLElement;
    if (item.offsetTop + item.offsetHeight < top) low = mid + 1;
    else high = mid;
  }
  const spans: HTMLElement[] = [];
  for (let i = low; i < count; i++) {
    const item = items[i] as HTMLElement;
    if (item.offsetTop > bottom) break;
    for (const span of item.getElementsByClassName("md-em")) spans.push(span as HTMLElement);
  }
  return spans;
}

/** A text node's line fragments: its text and first glyph's position on each line. */
function lineFragments(text: Text): { text: string; left: number; top: number }[] {
  const data = text.data;
  let end = data.length;
  while (end > 0 && (data[end - 1] === "\n" || data[end - 1] === "\r")) end--;
  const fragments: { text: string; left: number; top: number }[] = [];
  const range = document.createRange();
  const rectAt = (offset: number) => {
    range.setStart(text, offset);
    range.setEnd(text, offset + 1);
    return range.getClientRects()[0] ?? null;
  };
  let start = 0;
  while (start < end) {
    const first = rectAt(start);
    if (!first) return [];
    // The fragment runs until the first glyph on a later line.
    let low = start + 1;
    let high = end;
    const lastRect = rectAt(end - 1);
    if (lastRect && Math.abs(lastRect.top - first.top) < 1) low = end;
    else
      while (low < high) {
        const mid = (low + high) >> 1;
        const rect = rectAt(mid);
        if (rect && rect.top - first.top < 1) low = mid + 1;
        else high = mid;
      }
    fragments.push({ text: data.slice(start, low), left: first.left, top: first.top });
    start = low;
  }
  return fragments;
}

/** How far a copy's glyphs sit below its box top (the line box's half-leading). */
function liftFor(overlay: HTMLElement, state: SlantState, font: string): number | null {
  if (state.lift?.font === font) return state.lift.value;
  const probe = document.createElement("span");
  probe.className = "md-slant";
  probe.style.transform = "none";
  probe.textContent = "x";
  overlay.append(probe);
  const range = document.createRange();
  range.selectNodeContents(probe.firstChild!);
  const glyph = range.getClientRects()[0];
  const offset = glyph ? glyph.top - probe.getBoundingClientRect().top : null;
  probe.remove();
  if (offset === null) return null;
  state.lift = { font, value: offset };
  return offset;
}

/**
 * Keep the overlay's copies current: after every layer render, and on scroll,
 * resize, font loads and theme or appearance changes (the root's class/style).
 */
export function useItalicSlant(
  layerRef: RefObject<HTMLDivElement | null>,
  overlayRef: RefObject<HTMLDivElement | null>,
  /** The layer isn't painted (the compact preview shows): keep the copies as they are. */
  paused = false,
) {
  // One mutable state per mounted layer, stable across renders.
  const [stateRef] = useState<SlantState>(newSlantState);
  const pausedRef = useRef(paused);
  useLayoutEffect(() => {
    pausedRef.current = paused;
  });
  useLayoutEffect(() => {
    const layer = layerRef.current;
    const overlay = overlayRef.current;
    if (layer && overlay && !paused) safeSlant(layer, overlay, stateRef);
  });
  useLayoutEffect(() => {
    const layer = layerRef.current;
    const overlay = overlayRef.current;
    if (!layer || !overlay) return;
    let frame = 0;
    const schedule = (resetLift = false) => {
      if (resetLift) {
        stateRef.lift = null;
        stateRef.frame = [];
      }
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (!pausedRef.current) safeSlant(layer, overlay, stateRef);
      });
    };
    const onScroll = () => schedule();
    layer.addEventListener("scroll", onScroll, { passive: true });
    const resize =
      typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => schedule());
    resize?.observe(layer);
    const onFonts = () => schedule(true);
    document.fonts?.addEventListener?.("loadingdone", onFonts);
    const root =
      typeof MutationObserver === "undefined" ? null : new MutationObserver(() => schedule(true));
    root?.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "style", "data-theme"],
    });
    return () => {
      if (frame) cancelAnimationFrame(frame);
      layer.removeEventListener("scroll", onScroll);
      resize?.disconnect();
      document.fonts?.removeEventListener?.("loadingdone", onFonts);
      root?.disconnect();
      clearSlant(overlay, stateRef);
      stateRef.frame = [];
      stateRef.window = null;
    };
  }, [layerRef, overlayRef, stateRef]);
}

function safeSlant(layer: HTMLElement, overlay: HTMLElement, state: SlantState) {
  try {
    slantItalics(layer, overlay, state);
  } catch {
    // Measurement failed: leave every span on the dotted underline.
    for (const span of overlay.parentElement?.querySelectorAll("[data-slanted]") ?? [])
      span.removeAttribute("data-slanted");
    clearSlant(overlay, state);
    state.frame = [];
  }
}
