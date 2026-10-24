// Shared Radix collision boundary: an invisible fixed element inset by the
// `--omnigent-safe-*` variables (index.css). An element, not a number, lets the
// CSS values resolve live on every position update.

import { getEmbedRoot } from "@/lib/host";

type CollisionBoundary = Element | null | (Element | null)[];

let safeAreaBoundary: HTMLElement | null = null;

/** The shared boundary element; `mountSafeAreaCollisionBoundary` attaches it. */
export function getSafeAreaCollisionBoundary(): HTMLElement {
  if (safeAreaBoundary) return safeAreaBoundary;
  const el = document.createElement("div");
  el.style.position = "fixed";
  el.style.top = "var(--omnigent-safe-top, 0px)";
  el.style.right = "var(--omnigent-safe-right, 0px)";
  el.style.bottom = "var(--omnigent-safe-bottom, 0px)";
  el.style.left = "var(--omnigent-safe-left, 0px)";
  el.style.visibility = "hidden";
  el.style.pointerEvents = "none";
  el.setAttribute("aria-hidden", "true");
  safeAreaBoundary = el;
  return el;
}

/**
 * Attach the boundary to the Radix portal container (the embed root once one
 * is registered, else `document.body`), moving it when that container changes.
 * Call from a layout effect that runs before the popover positions itself.
 */
export function mountSafeAreaCollisionBoundary(): void {
  const container = getEmbedRoot() ?? document.body;
  const el = getSafeAreaCollisionBoundary();
  if (el.parentElement !== container) container.appendChild(el);
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
