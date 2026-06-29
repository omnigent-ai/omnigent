const IME_PROCESSING_KEY_CODE = 229;

type ImeKeyboardEvent = {
  nativeEvent: {
    isComposing?: boolean;
    keyCode?: number;
  };
};

export function isImeCompositionKeyEvent(
  event: ImeKeyboardEvent,
  isComposing: boolean = false,
): boolean {
  return (
    isComposing ||
    event.nativeEvent.isComposing === true ||
    event.nativeEvent.keyCode === IME_PROCESSING_KEY_CODE
  );
}
