const IME_PROCESSING_KEY_CODE = 229;

interface ImeKeyFlags {
  isComposing?: boolean;
  keyCode?: number;
}

interface ImeKeyboardEvent {
  nativeEvent: ImeKeyFlags;
}

function hasImeCompositionFlags(flags: ImeKeyFlags): boolean {
  return flags.isComposing === true || flags.keyCode === IME_PROCESSING_KEY_CODE;
}

export function isImeCompositionKeyEvent(event: ImeKeyboardEvent, isComposing = false): boolean {
  return isComposing || hasImeCompositionFlags(event.nativeEvent);
}

/**
 * The same check for a native ``KeyboardEvent`` instead of React's wrapper.
 *
 * xterm's custom key handler is a plain DOM listener, so it receives the
 * native event and cannot call {@link isImeCompositionKeyEvent}, whose
 * parameter is React-shaped (``event.nativeEvent``). Both entry points share
 * one predicate so the ``keyCode === 229`` fallback — still needed for IMEs
 * and browsers that leave ``isComposing`` unset on keydown — is defined once.
 *
 * :param event: A native ``KeyboardEvent`` (only the IME flags are read).
 * :param isComposing: Caller-tracked composition state, for callers that
 *     follow ``compositionstart`` / ``compositionend`` themselves.
 * :returns: ``true`` when the keystroke belongs to an in-flight composition.
 */
export function isImeCompositionNativeKeyEvent(event: ImeKeyFlags, isComposing = false): boolean {
  return isComposing || hasImeCompositionFlags(event);
}
