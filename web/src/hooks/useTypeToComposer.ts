// Typing while focus is in the conversation, or nowhere, starts a message: the
// keystroke moves focus to the chat composer, which then receives the character.
// Chords (Cmd/Ctrl/Alt), Space (which scrolls the transcript), keys that a
// waiting question or a dialog took, and IME composition are left alone.

import { type RefObject, useEffect, useRef } from "react";

import { isFocusInDialogOrMenu, isTextEntry } from "@/lib/hotkeys";
import { isImeCompositionKeyEvent } from "@/lib/ime";

/** Whether `event` should carry its character into the composer. */
export function typesIntoComposer(event: KeyboardEvent, conversation: HTMLElement | null): boolean {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return false;
  if (event.key.length !== 1 || event.key === " ") return false;
  if (isImeCompositionKeyEvent({ nativeEvent: event })) return false;
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== document.body) {
    if (!conversation?.contains(active) || isTextEntry(active)) return false;
  }
  return !isFocusInDialogOrMenu();
}

/**
 * Focus the composer when the user starts typing in the conversation.
 *
 * @param focusComposer Focus the composer; a no-op where it can't take text.
 * @param conversation The conversation area that counts as being in the chat.
 */
export function useTypeToComposer(
  focusComposer: () => void,
  conversation: RefObject<HTMLElement | null>,
  enabled = true,
): void {
  const latest = useRef(focusComposer);
  latest.current = focusComposer;

  useEffect(() => {
    if (!enabled) return;
    // Bubble phase on window, so a waiting question's capture-phase keys go first.
    const handler = (event: KeyboardEvent): void => {
      if (typesIntoComposer(event, conversation.current)) latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, conversation]);
}
