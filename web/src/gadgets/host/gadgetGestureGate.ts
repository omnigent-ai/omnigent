// Decides whether a frame call that acts for the viewer (an action request or a navigation)
// follows a real gesture inside the gadget, from host-side state only. A call is allowed when:
// - the browser reports transient user activation, or, without that API, the frame took focus in
//   the last few seconds;
// - the gadget's frame is the focused element, and no gesture reached Omnigent's own document
//   since it took focus;
// - the gesture has not been used: after each decision focus moves to the frame's container, and
//   calls are at least `MIN_CALL_INTERVAL_MS` apart.
// designs/GADGETS.md explains why each condition is needed.

const MIN_CALL_INTERVAL_MS = 1000;
// About how long browsers keep transient activation after a click.
const ACTIVATION_WINDOW_MS = 5000;
const HOST_GESTURES = ["pointerdown", "mousedown", "touchstart", "keydown"] as const;

export interface GadgetGestureGate {
  /** Whether a call that acts for the viewer may run now; a true answer uses up the gesture. */
  claim: () => boolean;
  /** Whether focus is inside the gadget's frame, where a selection the frame reports can come from. */
  frameFocused: () => boolean;
  dispose: () => void;
}

export interface GadgetGestureGateOptions {
  /** Holds the gadget's frame element and nothing else. Focus moves here after each decision. */
  frameHost: HTMLElement;
  /** Whether the page has transient activation; `null` when the browser can't tell. */
  hasActivation?: () => boolean | null;
  now?: () => number;
}

function pageHasActivation(): boolean | null {
  const activation = (navigator as { userActivation?: { isActive: boolean } }).userActivation;
  return activation ? activation.isActive : null;
}

export function createGadgetGestureGate({
  frameHost,
  hasActivation = pageHasActivation,
  now = Date.now,
}: GadgetGestureGateOptions): GadgetGestureGate {
  const ownerDocument = frameHost.ownerDocument;
  // Event order, not time: a Tab key press and the focus it moves can share a millisecond.
  let sequence = 0;
  let focusedAt: number | null = null;
  let focusedAtTime = Number.NEGATIVE_INFINITY;
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
      focusedAtTime = now();
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
      focusedAtTime = now();
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
      const activation = hasActivation();
      const activated =
        activation === null ? now() - focusedAtTime <= ACTIVATION_WINDOW_MS : activation;
      const allowed =
        focused &&
        focusedAt !== null &&
        focusedAt > lastHostGesture &&
        now() - lastCallAt >= MIN_CALL_INTERVAL_MS &&
        activated;
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
