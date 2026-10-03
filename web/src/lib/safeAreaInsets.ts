// Shared Radix collision boundary for popover primitives: an invisible fixed
// element inset from the viewport by the OS safe-area insets (the
// `--omnigent-safe-*` fold variables, index.css). Popovers positioned against
// it clamp — and size their `--radix-*-available-height` cap — at the
// safe-area line instead of the raw screen edge, and the CSS variables resolve
// live at every position update (getComputedStyle cannot absolutize the custom
// properties, so a rect-carrying element stands in for a numeric read).

type CollisionBoundary = Element | null | (Element | null)[];

let safeAreaBoundary: HTMLElement | null = null;

/** The shared safe-area boundary element, created and attached on first use. */
export function getSafeAreaCollisionBoundary(): HTMLElement {
  if (safeAreaBoundary?.isConnected) return safeAreaBoundary;
  const el = safeAreaBoundary ?? document.createElement("div");
  el.style.position = "fixed";
  el.style.top = "var(--omnigent-safe-top, 0px)";
  el.style.right = "var(--omnigent-safe-right, 0px)";
  el.style.bottom = "var(--omnigent-safe-bottom, 0px)";
  el.style.left = "var(--omnigent-safe-left, 0px)";
  el.style.visibility = "hidden";
  el.style.pointerEvents = "none";
  el.setAttribute("aria-hidden", "true");
  document.body.appendChild(el);
  safeAreaBoundary = el;
  return el;
}

/**
 * Fold the safe-area boundary into a Radix `collisionBoundary` value. Radix
 * intersects the rects of every listed boundary (and the viewport), so the
 * caller's own boundaries keep applying.
 */
export function withSafeAreaCollisionBoundary(
  boundary: CollisionBoundary | undefined,
): (Element | null)[] {
  const list = boundary == null ? [] : Array.isArray(boundary) ? boundary : [boundary];
  return [...list, getSafeAreaCollisionBoundary()];
}
