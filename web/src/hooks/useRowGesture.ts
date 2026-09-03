import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type SyntheticEvent,
  type TouchEvent as ReactTouchEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type DraggableSyntheticListeners,
  PointerSensor,
  type PointerSensorOptions,
} from "@dnd-kit/core";
import type { SwipeAction, SwipeActionPreferences } from "@/lib/swipeActionPreferences";

export const ROW_GESTURE_HOLD_MS = 400;
export const ROW_SWIPE_ACTIVATE_PX = 12;
export const ROW_SWIPE_COMMIT_PX = 72;
export const ROW_SWIPE_FLICK_MIN_PX = 48;
export const ROW_SWIPE_FLICK_VELOCITY_PX_MS = 0.5;
export const ROW_SWIPE_FLICK_WINDOW_MS = 100;
// Native touch slop is typically 8-10dp; 10px filters hold tremble while
// keeping a deliberate pull immediate.
export const ROW_DRAG_ACTIVATE_PX = 10;
// Marks the recognizer's own contextmenu dispatch so the mid-gesture guards
// can suppress the OS long-press contextmenu without eating their own.
export const ROW_MENU_SYNTHETIC = Symbol("row-menu-synthetic");

// Nested controls that own their own press (kebab menu trigger, pin button).
// Radix opens the dropdown on the trigger's pointerdown, so a row gesture
// resolving over it would stack an action dialog on the open menu.
const ROW_INTERACTIVE_CONTROL_SELECTOR =
  'button, input, select, textarea, [contenteditable]:not([contenteditable="false"])';

// How long a resolved gesture's trailing-click suppression stays armed. The
// browser's synthesized click lands within the same task chain as the release
// (milliseconds), so this window is generous for it — while an
// assistive-technology activation that dispatches only `click` arrives on a
// human timescale, well past it, and must NOT be consumed as "trailing".
export const ROW_CLICK_SUPPRESS_WINDOW_MS = 250;

const ROW_SCROLL_ACTIVATE_PX = 25;
const ROW_HOLD_TOLERANCE_PX = 20;
const ROW_SWIPE_MAX_PX = 96;
const ROW_SWIPE_RESIST = 1 / 3;
const ROW_SWIPE_MAX_SAMPLES = 8;

export type RowGesturePhase = "idle" | "pending" | "swipe" | "scroll" | "armed" | "drag";

interface ActiveRowGesture {
  pointerId: number;
  startX: number;
  startY: number;
  lastX: number;
  lastY: number;
  armX: number;
  armY: number;
  phase: Exclude<RowGesturePhase, "idle">;
  target: Element;
  sensorTarget: Element;
  actions: SwipeActionPreferences;
  swipeEnabled: boolean;
  movementSamples: RowMovementSample[];
}

interface RowMovementSample {
  x: number;
  at: number;
}

interface RowGestureDndState {
  shouldStartDrag: () => boolean;
}

export interface RowGestureDndData {
  rowGesture: RowGestureDndState;
}

interface RowGestureActivationContext {
  active: { data: { current?: unknown } };
}

type RowGestureReset = (cancelDrag: boolean, releasePending?: boolean) => void;

const rowGestureResets = new Set<RowGestureReset>();

function handleGlobalTouchStart(event: TouchEvent) {
  if (event.touches.length <= 1) return;
  for (const reset of rowGestureResets) reset(true);
}

function registerRowGestureReset(reset: RowGestureReset) {
  if (rowGestureResets.size === 0) document.addEventListener("touchstart", handleGlobalTouchStart);
  rowGestureResets.add(reset);
  return () => {
    rowGestureResets.delete(reset);
    if (rowGestureResets.size === 0) {
      document.removeEventListener("touchstart", handleGlobalTouchStart);
    }
  };
}

/** Clears the recognizer after dnd-kit has ended or cancelled its drag. */
export function finishActiveRowGesture(cancelledBeforeRelease = false) {
  for (const reset of rowGestureResets) reset(cancelledBeforeRelease, cancelledBeforeRelease);
}

const rowGestureActivators = [
  {
    eventName: "onPointerMove",
    handler: (
      { nativeEvent: event }: ReactPointerEvent,
      { onActivation }: PointerSensorOptions,
      { active }: RowGestureActivationContext,
    ) => {
      if (event.pointerType !== "touch" || !event.isPrimary) return false;
      const data = active.data.current as Partial<RowGestureDndData> | undefined;
      if (!data?.rowGesture?.shouldStartDrag()) return false;
      onActivation?.({ event });
      return true;
    },
  },
];

/**
 * A drag sensor that is instantiated only after the row recognizer chooses
 * drag. Based on PointerSensor, NOT TouchSensor: the gesture is tracked as a
 * pointer stream, and a TouchSensor base listens for touchmove/touchend —
 * in a Pointer-Events-only environment the release emits only pointerup, so
 * dnd-kit stayed active forever (row dimmed, later drags blocked).
 * PointerSensor ends and cancels on the document's pointerup / pointercancel,
 * which fire on real touch hardware too.
 */
export class RowGesturePointerSensor extends PointerSensor {
  static override activators = rowGestureActivators as unknown as typeof PointerSensor.activators;
}

function swipeOffset(deltaX: number): number {
  const direction = Math.sign(deltaX);
  const travel = Math.abs(deltaX);
  if (travel <= ROW_SWIPE_COMMIT_PX) return deltaX;
  const damped = ROW_SWIPE_COMMIT_PX + (travel - ROW_SWIPE_COMMIT_PX) * ROW_SWIPE_RESIST;
  return direction * Math.min(damped, ROW_SWIPE_MAX_PX);
}

function recordMovementSample(gesture: ActiveRowGesture, x: number, at: number) {
  const samples = gesture.movementSamples;
  const previous = samples.at(-1);
  if (previous?.at === at) {
    previous.x = x;
  } else {
    samples.push({ x, at });
  }
  const cutoff = at - ROW_SWIPE_FLICK_WINDOW_MS;
  while (samples.length > ROW_SWIPE_MAX_SAMPLES || (samples.length > 1 && samples[0].at < cutoff)) {
    samples.shift();
  }
}

function releaseVelocity(gesture: ActiveRowGesture, x: number, at: number): number {
  recordMovementSample(gesture, x, at);
  const first = gesture.movementSamples[0];
  const last = gesture.movementSamples.at(-1);
  if (!first || !last || first === last || last.at <= first.at) return 0;
  return (last.x - first.x) / (last.at - first.at);
}

function callDndListener(
  listeners: DraggableSyntheticListeners,
  name: string,
  event: SyntheticEvent,
) {
  const listener = listeners?.[name] as ((event: SyntheticEvent) => void) | undefined;
  listener?.(event);
}

export function useRowGesture({
  enabled,
  swipeEnabled,
  dragEnabled,
  actions,
  onAction,
  onLongPress,
  onDragStart,
  onCancel,
  onPickUp,
}: {
  enabled: boolean;
  swipeEnabled: boolean;
  dragEnabled: boolean;
  actions: SwipeActionPreferences;
  onAction: (action: Exclude<SwipeAction, "none">) => void;
  onLongPress: (point: { clientX: number; clientY: number }) => void;
  onDragStart?: () => void;
  onCancel?: () => void;
  onPickUp?: () => void;
}) {
  const [dx, setDx] = useState(0);
  const [phase, setPhase] = useState<RowGesturePhase>("idle");
  const [activeActions, setActiveActions] = useState<SwipeActionPreferences | null>(null);
  const state = useRef<ActiveRowGesture | null>(null);
  const holdTimer = useRef<number | null>(null);
  const suppressClick = useRef(false);
  const suppressionKeydown = useRef<((event: KeyboardEvent) => void) | null>(null);
  const suppressionTimer = useRef<number | null>(null);
  const suppressionReleaseCleanup = useRef<(() => void) | null>(null);
  const touchMoveGuard = useRef<(() => void) | null>(null);
  const dxFrame = useRef<number | null>(null);
  const pendingDx = useRef(0);

  const cancelDxFrame = useCallback(() => {
    if (dxFrame.current === null) return;
    cancelAnimationFrame(dxFrame.current);
    dxFrame.current = null;
  }, []);

  const scheduleDx = useCallback((next: number) => {
    pendingDx.current = next;
    if (dxFrame.current !== null) return;
    dxFrame.current = requestAnimationFrame(() => {
      dxFrame.current = null;
      setDx(pendingDx.current);
    });
  }, []);

  const clearHoldTimer = useCallback(() => {
    if (holdTimer.current === null) return;
    window.clearTimeout(holdTimer.current);
    holdTimer.current = null;
  }, []);

  // Chrome samples touch-action at touchstart, so the armed-state class swap
  // to touch-none can't stop an in-flight touch from being claimed as a native
  // pan. A held finger hasn't started a scroll yet, so its touchmoves are
  // still cancelable — preventDefault here forestalls the pan until the
  // gesture resolves to drag or resets. React's delegated listeners are
  // passive, so this has to be a real native listener.
  const armTouchMoveGuard = useCallback(() => {
    if (touchMoveGuard.current) return;
    const handler = (event: TouchEvent) => {
      if (event.cancelable) event.preventDefault();
    };
    // The OS long-press contextmenu hit-tests the element under the finger —
    // once the row menu opens there, that is the menu itself, past every
    // row-level guard. Unprevented it starts text selection and cancels the
    // pointer stream, so suppress it document-wide while the gesture owns the
    // touch. The recognizer's own tagged dispatch passes through.
    const contextMenuHandler = (event: Event) => {
      if (ROW_MENU_SYNTHETIC in event) return;
      event.preventDefault();
    };
    const selectStartHandler = (event: Event) => event.preventDefault();
    touchMoveGuard.current = () => {
      document.removeEventListener("touchmove", handler, {
        passive: false,
      } as EventListenerOptions);
      document.removeEventListener("contextmenu", contextMenuHandler, true);
      document.removeEventListener("selectstart", selectStartHandler, true);
    };
    document.addEventListener("touchmove", handler, { passive: false });
    document.addEventListener("contextmenu", contextMenuHandler, true);
    document.addEventListener("selectstart", selectStartHandler, true);
  }, []);

  const disarmTouchMoveGuard = useCallback(() => {
    const teardown = touchMoveGuard.current;
    if (!teardown) return;
    touchMoveGuard.current = null;
    teardown();
  }, []);

  const releaseCapture = useCallback((gesture: ActiveRowGesture | null) => {
    if (!gesture) return;
    try {
      if (gesture.target.hasPointerCapture(gesture.pointerId)) {
        gesture.target.releasePointerCapture(gesture.pointerId);
      }
    } catch {
      // The browser may have released capture during pointer cancellation.
    }
  }, []);

  const clearClickSuppression = useCallback(() => {
    suppressClick.current = false;
    suppressionReleaseCleanup.current?.();
    suppressionReleaseCleanup.current = null;
    if (suppressionTimer.current !== null) {
      window.clearTimeout(suppressionTimer.current);
      suppressionTimer.current = null;
    }
    const keydown = suppressionKeydown.current;
    if (!keydown) return;
    suppressionKeydown.current = null;
    document.removeEventListener("keydown", keydown, true);
  }, []);

  // Armed until the trailing click arrives, the next press/key clears it, or
  // the bounded post-release window elapses. Cancellation can precede the
  // actual finger lift (capture loss or dnd-kit Escape), so defer the timer
  // until that pointer ends; otherwise a held finger can outlive the window.
  const suppressTrailingClick = useCallback(
    (pendingPointerId?: number) => {
      clearClickSuppression();
      suppressClick.current = true;
      const keydown = () => clearClickSuppression();
      suppressionKeydown.current = keydown;
      document.addEventListener("keydown", keydown, { capture: true, once: true });

      const startTimer = () => {
        if (!suppressClick.current) return;
        suppressionTimer.current = window.setTimeout(
          clearClickSuppression,
          ROW_CLICK_SUPPRESS_WINDOW_MS,
        );
      };
      if (pendingPointerId === undefined) {
        startTimer();
        return;
      }

      const onPointerEnd = (event: PointerEvent) => {
        if (event.pointerId !== pendingPointerId) return;
        suppressionReleaseCleanup.current?.();
        suppressionReleaseCleanup.current = null;
        startTimer();
      };
      document.addEventListener("pointerup", onPointerEnd, true);
      document.addEventListener("pointercancel", onPointerEnd, true);
      suppressionReleaseCleanup.current = () => {
        document.removeEventListener("pointerup", onPointerEnd, true);
        document.removeEventListener("pointercancel", onPointerEnd, true);
      };
    },
    [clearClickSuppression],
  );

  const reset = useCallback(
    (cancelDrag = false, releasePending = cancelDrag, dispatchSensorCancel = cancelDrag) => {
      const gesture = state.current;
      if (!gesture) return;
      // A cancellation that interrupts a resolved gesture (swipe, armed,
      // drag) can still be followed by the browser's synthesized trailing
      // click — e.g. mid-swipe capture loss — which would navigate into the
      // row being swiped away. onPointerUp arms this itself for normal
      // releases; cancel paths must too.
      if (cancelDrag && gesture.phase !== "pending") {
        suppressTrailingClick(releasePending ? gesture.pointerId : undefined);
      }
      clearHoldTimer();
      releaseCapture(gesture);
      disarmTouchMoveGuard();
      cancelDxFrame();
      state.current = null;
      setDx(0);
      setPhase("idle");
      setActiveActions(null);
      if (cancelDrag) onCancel?.();
      if (dispatchSensorCancel && gesture.phase === "drag") {
        // The sensor listens on the owner document, so dispatch the synthetic
        // cancel there directly: when the row just unmounted (the main reason
        // no native pointercancel will follow), an event dispatched on the
        // detached element would never bubble to the document and dnd-kit
        // would keep the drag alive.
        (gesture.sensorTarget.ownerDocument ?? document).dispatchEvent(
          new Event("pointercancel", { bubbles: true, cancelable: true }),
        );
      }
    },
    [
      cancelDxFrame,
      clearHoldTimer,
      disarmTouchMoveGuard,
      onCancel,
      releaseCapture,
      suppressTrailingClick,
    ],
  );

  const consumeClick = useCallback(() => {
    if (!suppressClick.current) return false;
    clearClickSuppression();
    return true;
  }, [clearClickSuppression]);

  const setGesturePhase = useCallback(
    (gesture: ActiveRowGesture, next: ActiveRowGesture["phase"]) => {
      gesture.phase = next;
      setPhase(next);
    },
    [],
  );

  const capturePointer = useCallback((gesture: ActiveRowGesture) => {
    try {
      gesture.target.setPointerCapture(gesture.pointerId);
    } catch {
      // Capture can fail if the pointer ended in the timer callback's turn.
    }
  }, []);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent) => {
      clearClickSuppression();
      if (event.pointerType !== "touch") return;
      if (!event.isPrimary) {
        reset(true);
        return;
      }
      if (!enabled) return;
      const target = event.target;
      if (target instanceof Node && !event.currentTarget.contains(target)) return;
      reset();
      // A press on a nested control belongs to that control, not the row.
      const control =
        target instanceof Element ? target.closest(ROW_INTERACTIVE_CONTROL_SELECTOR) : null;
      if (control && event.currentTarget.contains(control)) return;
      const gesture: ActiveRowGesture = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        lastX: event.clientX,
        lastY: event.clientY,
        armX: event.clientX,
        armY: event.clientY,
        phase: "pending",
        target: event.currentTarget,
        sensorTarget: target instanceof Element ? target : event.currentTarget,
        // A live Settings change must affect the next gesture, not change the
        // meaning of a finger that is already down.
        actions,
        swipeEnabled,
        movementSamples: [{ x: event.clientX, at: event.timeStamp }],
      };
      state.current = gesture;
      // The recognizer has claimed this touch: prevent the pointer-down
      // default here — not unconditionally on the row link — so native
      // claimants (anchor drag, focus flash) stand down only for pointers the
      // recognizer actually tracks. When the recognizer is disabled or the
      // press belongs to a nested control, the default (and the plain tap's
      // compatibility click) is left alone.
      event.preventDefault();
      setActiveActions(actions);
      setPhase("pending");
      holdTimer.current = window.setTimeout(() => {
        if (state.current !== gesture || gesture.phase !== "pending") return;
        // A finger still creeping hasn't held still, it's scrolling slowly — and
        // arming would capture the pointer and take the scroll away.
        const drift = Math.hypot(gesture.lastX - gesture.startX, gesture.lastY - gesture.startY);
        if (drift > ROW_HOLD_TOLERANCE_PX) {
          holdTimer.current = null;
          setGesturePhase(gesture, "scroll");
          return;
        }
        holdTimer.current = null;
        gesture.armX = gesture.lastX;
        gesture.armY = gesture.lastY;
        setGesturePhase(gesture, "armed");
        capturePointer(gesture);
        armTouchMoveGuard();
        if (typeof navigator.vibrate === "function") navigator.vibrate(10);
        onPickUp?.();
        onLongPress({ clientX: gesture.lastX, clientY: gesture.lastY });
      }, ROW_GESTURE_HOLD_MS);
    },
    [
      actions,
      armTouchMoveGuard,
      capturePointer,
      clearClickSuppression,
      enabled,
      onLongPress,
      onPickUp,
      reset,
      setGesturePhase,
      swipeEnabled,
    ],
  );

  const onPointerMove = useCallback(
    (event: ReactPointerEvent) => {
      const gesture = state.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      const deltaX = event.clientX - gesture.startX;
      const deltaY = event.clientY - gesture.startY;
      const moved = event.clientX !== gesture.lastX || event.clientY !== gesture.lastY;
      gesture.lastX = event.clientX;
      gesture.lastY = event.clientY;

      if (gesture.phase === "armed") {
        if (
          !moved ||
          Math.hypot(event.clientX - gesture.armX, event.clientY - gesture.armY) <
            ROW_DRAG_ACTIVATE_PX
        ) {
          return;
        }
        if (dragEnabled) {
          onDragStart?.();
          setGesturePhase(gesture, "drag");
        } else {
          setGesturePhase(gesture, "scroll");
          releaseCapture(gesture);
          disarmTouchMoveGuard();
          onCancel?.();
        }
        return;
      }
      if (gesture.phase === "drag" || gesture.phase === "scroll") return;
      // Velocity only matters to the swipe commit, so sample it once the
      // armed / drag / scroll phases (which never read it) are ruled out.
      recordMovementSample(gesture, event.clientX, event.timeStamp);
      if (gesture.phase === "swipe") {
        // Reversing past the origin crosses into the other direction, which may
        // be configured inert. Rest the row there and stop claiming the gesture,
        // rather than translating with nothing revealed behind it.
        const reversedInto = deltaX < 0 ? gesture.actions.left : gesture.actions.right;
        if (reversedInto === "none") {
          scheduleDx(0);
          return;
        }
        event.preventDefault();
        scheduleDx(swipeOffset(deltaX));
        return;
      }

      const horizontal = Math.abs(deltaX);
      const vertical = Math.abs(deltaY);
      // Horizontal-dominant travel locks swipe at 12px. The regions overlap
      // (e.g. 18,17.5 satisfies both) — this check running first is what gives
      // swipe precedence; everything it declines waits for the 25px circle.
      if (horizontal >= ROW_SWIPE_ACTIVATE_PX && horizontal > vertical) {
        const action = deltaX < 0 ? gesture.actions.left : gesture.actions.right;
        if (gesture.swipeEnabled && action !== "none") {
          clearHoldTimer();
          setGesturePhase(gesture, "swipe");
          capturePointer(gesture);
          event.preventDefault();
          setDx(swipeOffset(deltaX));
          return;
        }
        // A wobble toward an inert direction is not a gesture: stay pending so
        // the trailing click still lands, until the 25px circle calls it a scroll.
      }
      if (Math.hypot(deltaX, deltaY) >= ROW_SCROLL_ACTIVATE_PX) {
        clearHoldTimer();
        setGesturePhase(gesture, "scroll");
      }
    },
    [
      capturePointer,
      clearHoldTimer,
      dragEnabled,
      disarmTouchMoveGuard,
      onDragStart,
      onCancel,
      releaseCapture,
      scheduleDx,
      setGesturePhase,
    ],
  );

  const onPointerUp = useCallback(
    (event: ReactPointerEvent) => {
      const gesture = state.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      let resolvedPhase = gesture.phase;
      const hasReleasePoint = Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
      const deltaX = hasReleasePoint ? event.clientX - gesture.startX : 0;
      const deltaY = hasReleasePoint ? event.clientY - gesture.startY : 0;
      if (
        resolvedPhase === "pending" &&
        Math.abs(deltaX) >= ROW_SWIPE_ACTIVATE_PX &&
        Math.abs(deltaX) > Math.abs(deltaY)
      ) {
        const releaseAction = deltaX < 0 ? gesture.actions.left : gesture.actions.right;
        if (gesture.swipeEnabled && releaseAction !== "none") resolvedPhase = "swipe";
      }
      const offset = resolvedPhase === "swipe" && hasReleasePoint ? swipeOffset(deltaX) : 0;
      const action =
        offset < 0 ? gesture.actions.left : offset > 0 ? gesture.actions.right : "none";
      const velocity = hasReleasePoint
        ? releaseVelocity(gesture, event.clientX, event.timeStamp)
        : 0;
      const isFlick =
        Math.abs(offset) >= ROW_SWIPE_FLICK_MIN_PX &&
        Math.sign(velocity) === Math.sign(offset) &&
        Math.abs(velocity) >= ROW_SWIPE_FLICK_VELOCITY_PX_MS;
      reset();

      if (resolvedPhase !== "pending") suppressTrailingClick();
      if (
        resolvedPhase === "swipe" &&
        (Math.abs(offset) >= ROW_SWIPE_COMMIT_PX || isFlick) &&
        action !== "none"
      ) {
        onAction(action);
      }
    },
    [onAction, reset, suppressTrailingClick],
  );

  const onPointerCancel = useCallback(
    (event: ReactPointerEvent) => {
      if (state.current?.pointerId !== event.pointerId) return;
      // This native cancel keeps bubbling to PointerSensor on the document;
      // only cancellation paths without a native event need a synthetic one.
      reset(true, false, false);
    },
    [reset],
  );

  const onLostPointerCapture = useCallback(
    (event: ReactPointerEvent) => {
      const gesture = state.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      // Ignore implicit capture ending on the touched child while capture is
      // transferred to the row; losing the row's own capture is terminal.
      if (event.nativeEvent.composedPath()[0] !== gesture.target) return;
      reset(true);
    },
    [reset],
  );

  const onTouchStart = useCallback(
    (event: ReactTouchEvent) => {
      if (event.touches.length > 1) reset(true);
    },
    [reset],
  );

  const dndData = useMemo<RowGestureDndState>(
    () => ({ shouldStartDrag: () => state.current?.phase === "drag" }),
    [],
  );

  const bindListeners = useCallback(
    (dragListeners: DraggableSyntheticListeners) => ({
      ...dragListeners,
      onPointerDown,
      onPointerMove: (event: ReactPointerEvent) => {
        onPointerMove(event);
        callDndListener(dragListeners, "onPointerMove", event);
      },
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture,
      onKeyDown: (event: ReactKeyboardEvent) => {
        clearClickSuppression();
        callDndListener(dragListeners, "onKeyDown", event);
      },
      onTouchStart: (event: ReactTouchEvent) => {
        onTouchStart(event);
        callDndListener(dragListeners, "onTouchStart", event);
      },
    }),
    [
      clearClickSuppression,
      onLostPointerCapture,
      onPointerCancel,
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onTouchStart,
    ],
  );

  useEffect(() => {
    if (!enabled) reset(true);
  }, [enabled, reset]);

  useEffect(() => registerRowGestureReset(reset), [reset]);

  useEffect(
    () => () => {
      reset(true);
      clearClickSuppression();
    },
    [clearClickSuppression, reset],
  );

  return {
    dx,
    phase,
    actions: activeActions,
    listeners: bindListeners,
    dndData,
    consumeClick,
    clearClickSuppression,
  };
}
