// CodeMirror estimates the height of wrapped lines it hasn't drawn from the content
// width it first measured, and never re-estimates them when that width changes.
// The viewer often first measures while its pane is still settling (a fraction of a
// pixel is enough), so the unseen lines' estimates are off and the document grows
// or shrinks as they're drawn: the scrollbar drifts under the wheel. This extension
// rebuilds the estimates once the content width has settled at a new value.
//
// CodeMirror has no public way to re-estimate for a new width (its "refresh" measure
// only re-estimates for a new line height), so the rebuild is `setState` with the
// same state. That keeps the document, selection, undo history and focused content
// element, and the scroll position is restored; but it recreates panels and plugins,
// so it waits until none is open and no composition or drag is in progress.

import { type EditorView, ViewPlugin, showPanel } from "@codemirror/view";

/** Frames a new width must hold before it counts as settled. */
export const SETTLED_FRAMES = 3;

/**
 * Whether wrapped-line estimates last built at `estimatedWidth` are stale at a
 * settled `width`. Any difference counts: line length is width over character width,
 * unrounded, so a fraction of a pixel moves long lines across a wrap.
 */
export function wrapEstimateIsStale(estimatedWidth: number, width: number): boolean {
  return width > 0 && width !== estimatedWidth;
}

/** Rebuilds a view's line-height estimates for its current width, keeping where the reader was. */
export function reestimateHeights(view: EditorView): void {
  const scroll = view.scrollSnapshot();
  view.setState(view.state);
  view.dispatch({ effects: scroll });
}

const contentWidth = (view: EditorView) => view.contentDOM.getBoundingClientRect().width;
// EditorView.lineWrapping marks the content; only wrapped lines' heights depend on width.
const isWrapping = (view: EditorView) => view.contentDOM.classList.contains("cm-lineWrapping");

// Whether a pointer button is down in this window, as while dragging a pane's edge.
// A release can go unseen once the window loses focus, so blur and a buttonless move end it too.
let pointerHeld = false;
const pointerReleaseListeners = new Set<() => void>();
if (typeof window !== "undefined") {
  const release = () => {
    if (!pointerHeld) return;
    pointerHeld = false;
    for (const listener of [...pointerReleaseListeners]) listener();
  };
  window.addEventListener("pointerdown", () => (pointerHeld = true), true);
  window.addEventListener("pointerup", release, true);
  window.addEventListener("pointercancel", release, true);
  window.addEventListener("blur", release);
  window.addEventListener(
    "pointermove",
    (event) => {
      if (event.buttons === 0) release();
    },
    true,
  );
}

/**
 * Whether a rebuild now would disturb the reader: a drag is in progress, a composition
 * (IME) has started, or a panel such as Find is open, which a rebuild would close.
 */
function rebuildWouldDisturb(view: EditorView): boolean {
  return (
    pointerHeld ||
    view.compositionStarted ||
    view.state.facet(showPanel).some((panel) => panel !== null)
  );
}

/**
 * Re-estimates wrapped lines' heights when the content width settles somewhere new:
 * it holds for a few animation frames and a rebuild wouldn't disturb the reader (a
 * drag rebuilds once, on release). Each settled change rebuilds once.
 */
export const reestimateWrappedHeightsOnSettledWidth = ViewPlugin.define((view) => {
  // The width the estimates were last built for; before layout it's 0, so the first
  // settled width always rebuilds them, whatever width CodeMirror first measured.
  let estimatedWidth = contentWidth(view);
  let frame = 0;
  let destroyed = false;
  let deferred = false;

  const settle = (width: number, heldFrames: number) => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const now = contentWidth(view);
      if (now !== width) settle(now, 0);
      else if (heldFrames + 1 < SETTLED_FRAMES) settle(width, heldFrames + 1);
      else check(width);
    });
  };
  const stopDeferring = () => {
    deferred = false;
    pointerReleaseListeners.delete(resume);
    view.contentDOM.removeEventListener("compositionend", resume);
  };
  // A release, a composition's end or any editor update (a panel closing) checks again.
  const resume = () => {
    if (!deferred) return;
    stopDeferring();
    if (!destroyed) settle(contentWidth(view), 0);
  };
  const check = (width: number) => {
    if (destroyed) return;
    if (!isWrapping(view) || !wrapEstimateIsStale(estimatedWidth, width)) {
      estimatedWidth = width;
      return;
    }
    if (rebuildWouldDisturb(view)) {
      deferred = true;
      pointerReleaseListeners.add(resume);
      view.contentDOM.addEventListener("compositionend", resume);
      return;
    }
    estimatedWidth = width;
    reestimateHeights(view);
  };

  const observer =
    typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => {
          stopDeferring();
          settle(contentWidth(view), 0);
        });
  observer?.observe(view.contentDOM);
  return {
    update: resume,
    destroy() {
      destroyed = true;
      cancelAnimationFrame(frame);
      stopDeferring();
      observer?.disconnect();
    },
  };
});
