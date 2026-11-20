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
 * value and selection live in memory, as another editor engine's would. A locked
 * surface refuses inserts, as a read-only draft does.
 */
export function createFakeComposerSurface(
  initial = "",
  { locked = false }: { locked?: boolean } = {},
): FakeComposerSurface {
  const element = document.createElement("div");
  element.contentEditable = "true";
  element.tabIndex = 0;
  element.setAttribute(COMPOSER_INPUT_ATTRIBUTE, "");
  document.body.append(element);
  let selection: [number, number] = [initial.length, initial.length];
  let reveals = 0;
  const inserts: FakeComposerSurface["inserts"] = [];
  const clamp = (offset: number) => Math.min(Math.max(offset, 0), surface.value.length);
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
      // As a textarea does: offsets clamp to the value, and a reversed range collapses.
      const clampedEnd = clamp(end);
      selection = [Math.min(clamp(start), clampedEnd), clampedEnd];
    },
    focus(options) {
      element.focus(options);
    },
    blur() {
      element.blur();
    },
    insertText(text, start, end) {
      if (locked) return false;
      // Clamped as the real surface clamps, and recorded clamped.
      const from = clamp(start);
      const to = clamp(end);
      inserts.push({ text, start: from, end: to });
      surface.value = surface.value.slice(0, from) + text + surface.value.slice(to);
      selection = [from + text.length, from + text.length];
      return true;
    },
    revealCaret() {
      reveals += 1;
    },
    unregister: () => unregister(),
  };
  const unregister = registerComposerSurface(surface);
  return surface;
}
