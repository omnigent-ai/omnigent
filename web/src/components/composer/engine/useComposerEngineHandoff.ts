// Switches a mounted composer between input engines without losing the user's place. The
// draft itself lives above the engines; this carries over which input was active, its
// selection and its scroll, and waits while a switch would interrupt the user.

import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import {
  COMPOSER_INPUT_ID_ATTRIBUTE,
  COMPOSER_INPUT_SELECTOR,
  composerTargetElement,
  composerTargetOf,
  type ComposerInputEngine,
  type ComposerTextTarget,
} from "./composerInput";

interface HandoffSnapshot {
  /** The active input's id ("tail" or a reply quote's), which both engines render. */
  inputId: string;
  /** The input's text, which the selection's offsets index. */
  value: string;
  selectionStart: number;
  selectionEnd: number;
  scrollTop: number;
  scrollLeft: number;
  focused: boolean;
}

function composerInputs(root: HTMLElement): Element[] {
  return Array.from(root.querySelectorAll(COMPOSER_INPUT_SELECTOR));
}

function inputIdOf(element: Element | null | undefined): string | null {
  return element?.getAttribute(COMPOSER_INPUT_ID_ATTRIBUTE) ?? null;
}

function inputWithId(root: HTMLElement, inputId: string): Element | undefined {
  return composerInputs(root).find((input) => inputIdOf(input) === inputId);
}

/**
 * The active input: the focused one, else `activeId` while it still exists, else the last
 * input (the tail), which is where the composer's owner falls back to as well.
 */
function capture(root: HTMLElement, activeId: string | null): HandoffSnapshot | null {
  const inputs = composerInputs(root);
  const focused = inputs.find((input) => input === document.activeElement);
  const remembered = activeId === null ? undefined : inputWithId(root, activeId);
  const active = focused ?? remembered ?? inputs.at(-1);
  const inputId = inputIdOf(active);
  const target = composerTargetOf(active);
  if (!target || inputId === null) return null;
  return {
    inputId,
    value: target.value,
    selectionStart: target.selectionStart,
    selectionEnd: target.selectionEnd,
    scrollTop: target.scrollTop,
    scrollLeft: target.scrollLeft,
    focused: focused !== undefined,
  };
}

function restore(root: HTMLElement, snapshot: HandoffSnapshot): ComposerTextTarget | null {
  // An input the new engine lacks, like a quote's in one document, hands over to the tail.
  const element = inputWithId(root, snapshot.inputId) ?? composerInputs(root).at(-1);
  const target = composerTargetOf(element);
  if (!target) return null;
  // The tail ends the one document, so a selection in it moves by the text before it. Any other
  // selection indexes text the new input doesn't hold, and the caret goes to the end.
  const found = inputIdOf(element) === snapshot.inputId;
  const { value } = target;
  const shift = value.length - snapshot.value.length;
  const nested = value.endsWith(snapshot.value) || snapshot.value.endsWith(value);
  const carried = found && nested && snapshot.selectionStart + shift >= 0;
  const end = value.length;
  target.setSelectionRange(
    carried ? snapshot.selectionStart + shift : end,
    carried ? snapshot.selectionEnd + shift : end,
  );
  // Scroll belongs to the old input's text, so it carries only when that text is unchanged.
  const same = found && value === snapshot.value;
  if (same) {
    target.scrollTop = snapshot.scrollTop;
    target.scrollLeft = snapshot.scrollLeft;
  }
  if (snapshot.focused) target.focus({ preventScroll: true });
  return target;
}

export interface ComposerHandoffOptions {
  /** Dictation is streaming a partial into the draft; switching would drop its region. */
  dictating?: boolean;
  /** The draft shown, such as its session; a new one forgets the last one's inputs. */
  draftKey?: string | null;
  /**
   * The input the owner treats as active ("tail" or a quote's id), which wins over the one
   * last focused: the owner moves it on its own, as when a quote is removed.
   */
  activeInputId?: string | null;
  /** The active input after a switch, now on the new engine, so the owner's refs stay current. */
  onActiveInputChange?: (target: ComposerTextTarget) => void;
  /** The engine rendered, told at mount and in the same commit as each switch. */
  onRenderedEngineChange?: (engine: ComposerInputEngine) => void;
}

/**
 * The engine to render for a composer whose root is `rootRef`. Follows `requested`, except
 * while the user is composing with an IME, dragging a selection, or dictating a partial;
 * the switch happens as soon as that ends, and is dropped if `requested` changes back.
 */
export function useComposerEngineHandoff(
  requested: ComposerInputEngine,
  rootRef: RefObject<HTMLElement | null>,
  {
    dictating = false,
    draftKey = null,
    activeInputId,
    onActiveInputChange,
    onRenderedEngineChange,
  }: ComposerHandoffOptions = {},
): ComposerInputEngine {
  const [rendered, setRendered] = useState(requested);
  // Bumped when a composition or drag ends, so a waiting switch is checked again.
  const [busyTick, setBusyTick] = useState(0);
  const switchWaiting = useRef(false);
  const composing = useRef(false);
  const dragging = useRef(false);
  const lastFocusedId = useRef<string | null>(null);
  const pending = useRef<HandoffSnapshot | null>(null);
  const onActiveInputChangeRef = useRef(onActiveInputChange);
  const onRenderedEngineChangeRef = useRef(onRenderedEngineChange);
  const activeInputIdRef = useRef(activeInputId);
  // Told at mount too, so an owner that started on another engine agrees from the first commit.
  const [mountedEngine] = useState(requested);
  useLayoutEffect(() => {
    onRenderedEngineChangeRef.current?.(mountedEngine);
  }, [mountedEngine]);
  useLayoutEffect(() => {
    onActiveInputChangeRef.current = onActiveInputChange;
    onRenderedEngineChangeRef.current = onRenderedEngineChange;
    activeInputIdRef.current = activeInputId;
    switchWaiting.current = requested !== rendered;
  });

  // Another draft starts with nothing remembered from the last one. A live composition or drag
  // still belongs to the DOM, so it isn't cleared here.
  useLayoutEffect(() => {
    lastFocusedId.current = null;
    pending.current = null;
  }, [draftKey]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    // Only a waiting switch needs to re-check, so typing and IME never re-render an idle composer.
    const settle = () => {
      if (switchWaiting.current) setBusyTick((tick) => tick + 1);
    };
    const onCompositionStart = () => {
      composing.current = true;
    };
    const onCompositionEnd = () => {
      composing.current = false;
      settle();
    };
    const onPointerDown = (event: PointerEvent) => {
      // A secondary-button press opens a context menu and never sends its pointerup.
      if (event.button !== 0) return;
      if (event.target instanceof Element && event.target.closest(COMPOSER_INPUT_SELECTOR)) {
        dragging.current = true;
      }
    };
    const onPointerUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      settle();
    };
    const onFocusIn = (event: FocusEvent) => {
      if (!(event.target instanceof Element) || !event.target.matches(COMPOSER_INPUT_SELECTOR)) {
        return;
      }
      lastFocusedId.current = inputIdOf(event.target);
    };
    root.addEventListener("compositionstart", onCompositionStart, true);
    root.addEventListener("compositionend", onCompositionEnd, true);
    root.addEventListener("pointerdown", onPointerDown, true);
    root.addEventListener("focusin", onFocusIn, true);
    window.addEventListener("pointerup", onPointerUp, true);
    window.addEventListener("pointercancel", onPointerUp, true);
    return () => {
      root.removeEventListener("compositionstart", onCompositionStart, true);
      root.removeEventListener("compositionend", onCompositionEnd, true);
      root.removeEventListener("pointerdown", onPointerDown, true);
      root.removeEventListener("focusin", onFocusIn, true);
      window.removeEventListener("pointerup", onPointerUp, true);
      window.removeEventListener("pointercancel", onPointerUp, true);
    };
  }, [rootRef]);

  useLayoutEffect(() => {
    if (requested === rendered) return;
    if (composing.current || dragging.current || dictating) return;
    const root = rootRef.current;
    const activeId =
      activeInputIdRef.current === undefined ? lastFocusedId.current : activeInputIdRef.current;
    pending.current = root ? capture(root, activeId) : null;
    setRendered(requested);
    onRenderedEngineChangeRef.current?.(requested);
  }, [requested, rendered, dictating, busyTick, draftKey, rootRef]);

  // After the new engine has mounted its inputs, put the user back where they were.
  useLayoutEffect(() => {
    const snapshot = pending.current;
    const root = rootRef.current;
    pending.current = null;
    if (!snapshot || !root) return;
    const target = restore(root, snapshot);
    // The restored input stays the remembered one for the next switch, focused or not.
    lastFocusedId.current = target ? inputIdOf(composerTargetElement(target)) : snapshot.inputId;
    if (target) onActiveInputChangeRef.current?.(target);
  }, [rendered, rootRef]);

  return rendered;
}
