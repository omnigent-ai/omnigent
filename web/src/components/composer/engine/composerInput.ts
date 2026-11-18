// The contract every chat composer input engine implements. Composer logic (send, history,
// completion, dictation, paste) reads and edits the draft through a ComposerTextTarget, so it
// works the same whether the editable surface is today's <textarea> or another editor.

/**
 * An editable surface that isn't a <textarea>. It mirrors the textarea API the composer uses,
 * with the same semantics: selection survives blur, `setSelectionRange` neither focuses nor
 * scrolls, `focus` keeps the selection, and `insertText` is an undoable user edit that reports
 * the new value exactly once.
 */
export interface ComposerEditorSurface {
  /** The focusable node, marked with `data-composer-input`. */
  readonly element: HTMLElement;
  readonly isConnected: boolean;
  readonly value: string;
  readonly selectionStart: number;
  readonly selectionEnd: number;
  scrollTop: number;
  scrollLeft: number;
  setSelectionRange: (start: number, end: number) => void;
  focus: (options?: FocusOptions) => void;
  blur: () => void;
  /** Replace `[start, end)` as typed input. */
  insertText: (text: string, start: number, end: number) => void;
  /** Scroll the caret's line into view. */
  revealCaret: () => void;
}

/** What the composer edits: a <textarea> as-is, or another engine's surface. */
export type ComposerTextTarget = HTMLTextAreaElement | ComposerEditorSurface;

/** A paste as the composer sees it; React's textarea paste events satisfy it. */
export interface ComposerPasteEvent {
  readonly clipboardData: DataTransfer;
  readonly currentTarget: ComposerTextTarget;
  preventDefault: () => void;
}

/** Marks the focusable node of every composer input, whichever engine renders it. */
export const COMPOSER_INPUT_ATTRIBUTE = "data-composer-input";
export const COMPOSER_INPUT_SELECTOR = `[${COMPOSER_INPUT_ATTRIBUTE}]`;
/** Spread onto an engine's focusable node to mark it. */
export const COMPOSER_INPUT_MARKER = { [COMPOSER_INPUT_ATTRIBUTE]: "" } as const;
/** A composer input that takes typing now. */
export const ENABLED_COMPOSER_INPUT_SELECTOR = `${COMPOSER_INPUT_SELECTOR}:not(:disabled):not([aria-disabled="true"])`;

const surfaces = new WeakMap<Element, ComposerEditorSurface>();

/** Let `composerTargetOf` find an engine's surface from its focusable node. */
export function registerComposerSurface(surface: ComposerEditorSurface): () => void {
  surfaces.set(surface.element, surface);
  return () => {
    if (surfaces.get(surface.element) === surface) surfaces.delete(surface.element);
  };
}

export function isComposerTextarea(target: ComposerTextTarget): target is HTMLTextAreaElement {
  return target instanceof HTMLTextAreaElement;
}

/** The DOM node that holds focus for `target`. */
export function composerTargetElement(target: ComposerTextTarget): HTMLElement {
  return isComposerTextarea(target) ? target : target.element;
}

export function isComposerTargetFocused(target: ComposerTextTarget): boolean {
  return composerTargetElement(target) === document.activeElement;
}

/** Whether `element` is a composer input, of any engine. */
export function isComposerInput(element: Element | null | undefined): boolean {
  return element?.hasAttribute(COMPOSER_INPUT_ATTRIBUTE) === true;
}

/** The composer target whose focusable node is `element`, if it is one. */
export function composerTargetOf(element: Element | null | undefined): ComposerTextTarget | null {
  if (!element || !isComposerInput(element)) return null;
  if (element instanceof HTMLTextAreaElement) return element;
  return surfaces.get(element) ?? null;
}
