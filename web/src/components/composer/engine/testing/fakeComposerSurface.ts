import {
  COMPOSER_INPUT_ATTRIBUTE,
  registerComposerSurface,
  type ComposerEditorSurface,
} from "../composerInput";

export interface FakeComposerSurface extends ComposerEditorSurface {
  value: string;
  /** Calls to `insertText`, in order. */
  readonly inserts: { text: string; start: number; end: number }[];
  readonly reveals: number;
  /** Stop `composerTargetOf` from finding this surface. */
  unregister: () => void;
}

/**
 * A non-textarea composer surface for tests: a focusable, marked element whose
 * value and selection live in memory, as another editor engine's would.
 */
export function createFakeComposerSurface(initial = ""): FakeComposerSurface {
  const element = document.createElement("div");
  element.contentEditable = "true";
  element.tabIndex = 0;
  element.setAttribute(COMPOSER_INPUT_ATTRIBUTE, "");
  document.body.append(element);
  let selection: [number, number] = [initial.length, initial.length];
  let reveals = 0;
  const inserts: FakeComposerSurface["inserts"] = [];
  const surface: FakeComposerSurface = {
    element,
    value: initial,
    inserts,
    get reveals() {
      return reveals;
    },
    get isConnected() {
      return element.isConnected;
    },
    get selectionStart() {
      return selection[0];
    },
    get selectionEnd() {
      return selection[1];
    },
    scrollTop: 0,
    scrollLeft: 0,
    setSelectionRange(start, end) {
      selection = [start, end];
    },
    focus(options) {
      element.focus(options);
    },
    blur() {
      element.blur();
    },
    insertText(text, start, end) {
      inserts.push({ text, start, end });
      surface.value = surface.value.slice(0, start) + text + surface.value.slice(end);
      selection = [start + text.length, start + text.length];
    },
    revealCaret() {
      reveals += 1;
    },
    unregister: () => unregister(),
  };
  const unregister = registerComposerSurface(surface);
  return surface;
}
