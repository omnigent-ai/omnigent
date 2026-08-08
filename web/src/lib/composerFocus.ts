/**
 * Focus hand-off from a closing overlay to whichever composer the page lands on.
 *
 * A modal overlay traps focus: while it is open (and while its close animation
 * plays), any element that takes focus outside the overlay is immediately pulled
 * back in. A destination that focuses its own composer as it mounts — the
 * new-session landing screen, the in-session composer — therefore loses that
 * focus when the navigation came from an overlay, and the user ends up on
 * `<body>` with nowhere to type. The overlay instead requests focus once it is
 * gone, and the mounted composer takes it.
 *
 * Set-based so Strict-Mode double-mounts dedupe. Requesting focus with no
 * composer mounted is a no-op.
 */
import { useEffect } from "react";
import type { RefObject } from "react";

type ComposerFocusListener = () => void;

const listeners = new Set<ComposerFocusListener>();

/** Ask the mounted composer to take focus. */
export function requestComposerFocus(): void {
  for (const listener of listeners) {
    try {
      listener();
    } catch (err) {
      console.warn("[composer-focus] listener threw:", err);
    }
  }
}

/** Subscribe a composer to focus requests; returns an unsubscribe. */
export function onComposerFocusRequest(listener: ComposerFocusListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Focus `ref` whenever an overlay hands focus back to the page.
 *
 * `enabled` gates the subscription — pass `false` where programmatic focus is
 * unwanted (mobile, where it summons the software keyboard).
 */
export function useComposerFocusRequests(
  ref: RefObject<HTMLTextAreaElement | null>,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled) return;
    return onComposerFocusRequest(() => ref.current?.focus());
  }, [ref, enabled]);
}
