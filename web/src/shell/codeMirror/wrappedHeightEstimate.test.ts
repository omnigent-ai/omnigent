import { history, undo, undoDepth } from "@codemirror/commands";
import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SETTLED_FRAMES,
  reestimateWrappedHeightsOnSettledWidth,
  wrapEstimateIsStale,
} from "./wrappedHeightEstimate";

describe("wrapEstimateIsStale", () => {
  it("counts any settled change, a fraction of a pixel included, but not an unlaid-out view", () => {
    expect(wrapEstimateIsStale(385.9, 385.2)).toBe(true);
    expect(wrapEstimateIsStale(0, 385.2)).toBe(true);
    expect(wrapEstimateIsStale(385.2, 385.2)).toBe(false);
    expect(wrapEstimateIsStale(385.2, 0)).toBe(false);
  });
});

describe("reestimateWrappedHeightsOnSettledWidth", () => {
  let observers: { callback: ResizeObserverCallback }[] = [];
  let frames: FrameRequestCallback[] = [];
  let width = 0;

  beforeEach(() => {
    observers = [];
    frames = [];
    width = 0;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        callback: ResizeObserverCallback;
        constructor(callback: ResizeObserverCallback) {
          this.callback = callback;
          observers.push(this);
        }
        observe() {}
        disconnect() {}
      },
    );
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => {
      frames[id - 1] = () => {};
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({ width, height: 100 }) as DOMRect,
    );
    // CodeMirror's own measuring reads text geometry, which jsdom doesn't lay out.
    Range.prototype.getClientRects ??= () => [] as unknown as DOMRectList;
    Range.prototype.getBoundingClientRect ??= () => ({ width: 0, height: 0 }) as DOMRect;
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const view = (wrapping: boolean) =>
    new EditorView({
      state: EditorState.create({
        doc: "word ".repeat(400),
        extensions: [
          history(),
          wrapping ? EditorView.lineWrapping : [],
          reestimateWrappedHeightsOnSettledWidth,
        ],
      }),
      parent: document.body,
    });
  /** The pane resizes to `next`, and later animation frames find it there. */
  const resizeTo = (next: number) => {
    width = next;
    // CodeMirror watches sizes too; a resize reaches every observer.
    for (const observer of observers) observer.callback([], {} as ResizeObserver);
  };
  const nextFrame = () => {
    const pending = frames;
    frames = [];
    pending.forEach((frame) => frame(0));
  };
  /** Enough frames for a width to count as settled. */
  const nextFrames = () => {
    for (let index = 0; index < SETTLED_FRAMES + 1; index++) nextFrame();
  };

  it("rebuilds the estimates once a new width settles, and keeps the scroll position", () => {
    const editor = view(true);
    const setState = vi.spyOn(editor, "setState");
    const dispatch = vi.spyOn(editor, "dispatch");
    const state = editor.state;

    resizeTo(385.9);
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(1);
    expect(setState).toHaveBeenCalledWith(state);
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ effects: expect.anything() }));

    // The rebuilt view's own plugin now knows the width; settling there again does nothing.
    resizeTo(385.9);
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(1);

    // A fraction of a pixel narrower is enough to make the estimates stale.
    resizeTo(385.2);
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(2);
    editor.destroy();
  });

  it("keeps an editor's focus, selection and undo history through the rebuild", () => {
    const editor = view(true);
    editor.dispatch({ changes: { from: 0, insert: "typed " } });
    editor.dispatch({ selection: EditorSelection.range(2, 5) });
    editor.focus();
    const setState = vi.spyOn(editor, "setState");

    resizeTo(385.9);
    nextFrames();

    expect(setState).toHaveBeenCalledTimes(1);
    expect(editor.hasFocus).toBe(true);
    expect(editor.state.selection.main).toMatchObject({ from: 2, to: 5 });
    expect(undoDepth(editor.state)).toBe(1);
    undo(editor);
    expect(editor.state.doc.toString().startsWith("typed ")).toBe(false);
    editor.destroy();
  });

  it("waits from a composition's start, before its first edit, until it ends", () => {
    const editor = view(true);
    const setState = vi.spyOn(editor, "setState");

    editor.contentDOM.dispatchEvent(new CompositionEvent("compositionstart"));
    expect(editor.composing).toBe(false); // CodeMirror counts it composing only once it edits.
    resizeTo(385.9);
    nextFrames();
    expect(setState).not.toHaveBeenCalled();

    editor.contentDOM.dispatchEvent(new CompositionEvent("compositionend"));
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(1);
    editor.destroy();
  });

  it("rebuilds once after a drag, however many widths it passed through", () => {
    const editor = view(true);
    const setState = vi.spyOn(editor, "setState");
    resizeTo(400);
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("pointerdown"));
    for (const dragWidth of [398, 396, 394, 392, 390]) {
      resizeTo(dragWidth);
      nextFrames();
    }
    expect(setState).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("pointerup"));
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(2);
    editor.destroy();
  });

  it.each([
    ["the window loses focus", () => window.dispatchEvent(new Event("blur"))],
    [
      "the pointer comes back with no button held",
      () => window.dispatchEvent(new MouseEvent("pointermove", { buttons: 0 })),
    ],
  ])("ends a drag whose release went unseen once %s", (_label, endDrag) => {
    const editor = view(true);
    const setState = vi.spyOn(editor, "setState");
    window.dispatchEvent(new Event("pointerdown"));
    window.dispatchEvent(new MouseEvent("pointermove", { buttons: 1 }));
    resizeTo(385.9);
    nextFrames();
    expect(setState).not.toHaveBeenCalled();

    endDrag();
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(1);
    resizeTo(385.2);
    nextFrames();
    expect(setState).toHaveBeenCalledTimes(2);
    editor.destroy();
  });

  it("waits for the width to hold for a frame before rebuilding", () => {
    const editor = view(true);
    const setState = vi.spyOn(editor, "setState");

    resizeTo(400);
    nextFrame();
    width = 390; // Still moving before it has held for long enough.
    for (let index = 0; index < SETTLED_FRAMES - 1; index++) nextFrame();
    expect(setState).not.toHaveBeenCalled();
    editor.destroy();
  });

  it("leaves an unwrapped view alone, whose estimates don't depend on width", () => {
    const editor = view(false);
    const setState = vi.spyOn(editor, "setState");

    resizeTo(385.9);
    nextFrames();
    resizeTo(300);
    nextFrames();
    expect(setState).not.toHaveBeenCalled();
    editor.destroy();
  });
});
