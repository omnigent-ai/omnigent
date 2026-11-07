// While any CSS animation plays, Chromium draws every display frame. Indicators
// that animate for as long as a session runs are paused instead, and one shared
// timer advances them together; the keyframes stay in CSS for reduced motion.

// Ten steps a second still reads as motion, at a sixth of a 60 Hz display's frames.
export const ANIMATION_STEP_MS = 100;

const elements = new Set<Element>();
let intervalId: ReturnType<typeof setInterval> | null = null;

function step(): void {
  // A ref's null call on unmount doesn't name the element, so prune by connection.
  for (const element of elements) {
    if (!element.isConnected) elements.delete(element);
  }
  if (elements.size === 0) {
    if (intervalId !== null) clearInterval(intervalId);
    intervalId = null;
    return;
  }
  if (document.hidden) return;
  // One shared time keeps every indicator in phase, across remounts too.
  const now = performance.now();
  // A registered element's subtree can contain another registered element.
  const animations = new Set<Animation>();
  for (const element of elements) {
    for (const animation of element.getAnimations({ subtree: true })) animations.add(animation);
  }
  for (const animation of animations) {
    // Reduced motion runs each animation once; let it finish on its own.
    if (animation.effect?.getTiming().iterations !== Infinity) continue;
    animation.pause();
    animation.currentTime = now;
  }
}

/** Ref callback: steps the infinite CSS animations of an element and its subtree
 *  every {@link ANIMATION_STEP_MS} instead of every display frame. */
export function throttleAnimations(element: Element | null): void {
  if (!element || typeof element.getAnimations !== "function") return;
  elements.add(element);
  intervalId ??= setInterval(step, ANIMATION_STEP_MS);
}
