// The contract every chat composer input engine implements. Composer logic (send, history,
// completion, dictation, paste) reads and edits the draft through a ComposerTextTarget, so it
// works the same whether the editable surface is today's <textarea> or another editor.

import type { Ref } from "react";

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
  /**
   * Replace `[start, end)` as typed input. Returns false when the input is locked
   * (read-only or disabled) and nothing changed.
   */
  insertText: (text: string, start: number, end: number) => boolean;
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
/** Names one input within its composer, stable across engines. */
export const COMPOSER_INPUT_ID_ATTRIBUTE = "data-composer-input-id";
/** The id of the composer's own input, the one after any reply quotes. */
export const COMPOSER_TAIL_INPUT_ID = "tail";
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

/** A keydown as composer logic sees it; React's textarea events satisfy it. */
export interface ComposerKeyboardEvent {
  readonly key: string;
  readonly code: string;
  readonly altKey: boolean;
  readonly ctrlKey: boolean;
  readonly metaKey: boolean;
  readonly shiftKey: boolean;
  readonly repeat: boolean;
  readonly defaultPrevented: boolean;
  readonly nativeEvent: KeyboardEvent;
  readonly currentTarget: ComposerTextTarget;
  preventDefault: () => void;
  stopPropagation: () => void;
}

/** A change, selection, focus or scroll on a composer input; React's textarea events satisfy it. */
export interface ComposerInputEvent {
  readonly currentTarget: ComposerTextTarget;
  /** An engine's undo or redo made this change; a textarea reports it as its `inputType`. */
  readonly fromHistory?: boolean;
  readonly nativeEvent?: Event;
}

/** Whether `event` is an undo or redo, on either engine. */
export function isHistoryInput(event: ComposerInputEvent): boolean {
  if (event.fromHistory) return true;
  const inputType = (event.nativeEvent as InputEvent | undefined)?.inputType;
  return inputType === "historyUndo" || inputType === "historyRedo";
}

/**
 * `compact` grows with the draft up to the composer's cap, then scrolls. `unbounded` grows
 * with it, for an input whose surroundings scroll.
 */
export type ComposerInputHeightMode = "compact" | "unbounded";

/** Which engine renders the composer's editable surface. */
export type ComposerInputEngine = "textarea" | "codemirror";

/** What every input engine accepts: the subset of textarea props the composer uses. */
export interface ComposerInputProps {
  ref?: Ref<ComposerTextTarget>;
  value?: string;
  /** The starting text of an uncontrolled input. */
  defaultValue?: string;
  placeholder?: string;
  disabled?: boolean;
  readOnly?: boolean;
  autoFocus?: boolean;
  id?: string;
  className?: string;
  /** How tall the input is; `compact` when unset. */
  heightMode?: ComposerInputHeightMode;
  /** Which input this is within its composer, so an engine switch finds it again. */
  inputId?: string;
  /** Tagged @-mention tokens to present as one unit, such as "@README.md". */
  mentionTokens?: readonly string[];
  /** The slash command or skill the draft starts with, such as "/context", to tint. */
  commandToken?: string | null;
  onChange?: (event: ComposerInputEvent) => void;
  onSelect?: (event: ComposerInputEvent) => void;
  onFocus?: (event: ComposerInputEvent) => void;
  onBlur?: (event: ComposerInputEvent) => void;
  onScroll?: (event: ComposerInputEvent) => void;
  onKeyDown?: (event: ComposerKeyboardEvent) => void;
  onPaste?: (event: ComposerPasteEvent) => void;
  onCompositionStart?: () => void;
  onCompositionEnd?: () => void;
  "aria-label"?: string;
  "aria-keyshortcuts"?: string;
  "aria-describedby"?: string;
  /** A menu the input drives, such as a slash command's choices, and its highlighted row. */
  role?: "combobox";
  "aria-autocomplete"?: "list";
  "aria-controls"?: string;
  "aria-expanded"?: boolean;
  "aria-activedescendant"?: string;
  "data-testid"?: string;
  "data-slash-command"?: string;
  "data-has-draft"?: string;
}
