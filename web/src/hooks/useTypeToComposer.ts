// Typing while focus is in the conversation, or nowhere, starts a message: the
// keystroke moves focus to the chat composer, which then receives the character.
// Chords (Cmd/Ctrl/Alt), Space (which scrolls the transcript), keys that a
// waiting question or a dialog took, a fullscreen view, a focused dropdown's
// type-to-select, and IME composition are left alone.

import { type RefObject, useEffect, useRef } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isFocusInDialogOrMenu, isKeyOwningOverlayOpen, isTextEntry } from "@/lib/hotkeys";
import { isImeCompositionKeyEvent } from "@/lib/ime";

/** Whether `event` should carry its character into the composer. */
export function typesIntoComposer(event: KeyboardEvent, conversation: HTMLElement | null): boolean {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return false;
  if (event.key.length !== 1 || event.key === " ") return false;
  if (isImeCompositionKeyEvent({ nativeEvent: event })) return false;
  const active = document.activeElement;
  // A click on transcript text can focus an embed's root, which counts as no control.
  if (active instanceof HTMLElement && active !== document.body && active !== getEmbedRoot()) {
    if (!conversation?.contains(active) || isTextEntry(active)) return false;
    // A native dropdown picks its option by the typed letter.
    if (active instanceof HTMLSelectElement) return false;
  }
  // A fullscreen table or diagram covers the composer while focus stays behind it.
  return !isFocusInDialogOrMenu() && !isKeyOwningOverlayOpen();
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
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!enabled) return;
    // Bubble phase on window, so a waiting question's capture-phase keys go first.
    const handler = (event: KeyboardEvent): void => {
      // Embedded, typing in the host page around the app stays there.
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      if (typesIntoComposer(event, conversation.current)) latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, conversation, isEmbedded]);
}
