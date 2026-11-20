// Decides whether a frame call that acts for the viewer (an action request or a navigation)
// follows a real gesture inside the thingy. It reads only host-side state: the thingy's own script
// runs in the frame and can call the SDK at any time, so nothing the frame reports counts.
//
// A call is allowed only when all of these hold:
// - The browser reports transient user activation (`navigator.userActivation.isActive`). A click
//   or key press anywhere in the page sets it for a few seconds, so on its own it proves nothing.
// - The thingy's frame is the focused element. A click or Tab into the frame focuses it, a click
//   elsewhere in Omnigent moves focus away, and a cross-origin frame cannot take focus by script
//   without a gesture of its own.
// - No gesture reached Omnigent's own document since the frame took focus. Some host controls
//   keep focus where it was on mousedown, so focus alone could outlast a click elsewhere.
// - The gesture has not been used yet. The host cannot see clicks inside the frame, so after each
//   decision it moves focus out of the frame (to the frame's container, so Tab goes back in); the
//   next gesture inside focuses the frame again, which the host does see. A frame that refocuses
//   itself needs its own recent activation, and calls are spaced at least `MIN_CALL_INTERVAL_MS`
//   apart, which bounds what one real click can start.

const MIN_CALL_INTERVAL_MS = 1000;
const HOST_GESTURES = ["pointerdown", "mousedown", "touchstart", "keydown"] as const;

export interface ThingyGestureGate {
  /** Whether a call that acts for the viewer may run now; a true answer uses up the gesture. */
  claim: () => boolean;
  /** Whether focus is inside the thingy's frame, where a selection the frame reports can come from. */
  frameFocused: () => boolean;
  dispose: () => void;
}

export interface ThingyGestureGateOptions {
  /** Holds the thingy's frame element and nothing else. Focus moves here after each decision. */
  frameHost: HTMLElement;
  hasActivation?: () => boolean;
  now?: () => number;
}

/** Browsers without the API rely on focus alone. */
function pageHasActivation(): boolean {
  const activation = (navigator as { userActivation?: { isActive: boolean } }).userActivation;
  return !activation || activation.isActive;
}

export function createThingyGestureGate({
  frameHost,
  hasActivation = pageHasActivation,
  now = Date.now,
}: ThingyGestureGateOptions): ThingyGestureGate {
  const ownerDocument = frameHost.ownerDocument;
  // Event order, not time: a Tab key press and the focus it moves can share a millisecond.
  let sequence = 0;
  let focusedAt: number | null = null;
  let lastHostGesture = 0;
  let lastCallAt = Number.NEGATIVE_INFINITY;

  const frameFocused = () => {
    const active = ownerDocument.activeElement;
    return active !== null && active !== frameHost && frameHost.contains(active);
  };
  const onFocusIn = (event: FocusEvent) => {
    if (event.target !== frameHost) {
      sequence += 1;
      focusedAt = sequence;
    }
  };
  // Chrome fires no focusin on the frame element when a click moves focus into the frame; Omnigent's
  // window only loses focus, with the frame already its active element. Element blur events do not
  // bubble, so this non-capturing listener hears only the window's own.
  const view = ownerDocument.defaultView;
  const onWindowBlur = () => {
    if (frameFocused()) {
      sequence += 1;
      focusedAt = sequence;
    }
  };
  // Untrusted events count too: only Omnigent's own scripts can dispatch them in its document,
  // and counting them only makes the gate stricter.
  const onHostGesture = () => {
    sequence += 1;
    lastHostGesture = sequence;
  };

  frameHost.addEventListener("focusin", onFocusIn);
  view?.addEventListener("blur", onWindowBlur);
  for (const type of HOST_GESTURES) {
    ownerDocument.addEventListener(type, onHostGesture, true);
  }

  return {
    claim: () => {
      const focused = frameFocused();
      const allowed =
        focused &&
        focusedAt !== null &&
        focusedAt > lastHostGesture &&
        now() - lastCallAt >= MIN_CALL_INTERVAL_MS &&
        hasActivation();
      if (focused) {
        focusedAt = null;
        frameHost.focus({ preventScroll: true });
      }
      if (allowed) {
        lastCallAt = now();
      }
      return allowed;
    },
    frameFocused,
    dispose: () => {
      frameHost.removeEventListener("focusin", onFocusIn);
      view?.removeEventListener("blur", onWindowBlur);
      for (const type of HOST_GESTURES) {
        ownerDocument.removeEventListener(type, onHostGesture, true);
      }
    },
  };
}
