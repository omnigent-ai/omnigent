// One polite live region for changes in flight, so a screen reader hears that a change
// started, landed or failed even when the element it changed moved or went away.

let region: HTMLElement | null = null;

function liveRegion(): HTMLElement | null {
  if (typeof document === "undefined") return null;
  if (region?.isConnected) return region;
  region = document.createElement("div");
  // A bare live region, not a status role, so it doesn't stand for any one view's status.
  region.setAttribute("aria-live", "polite");
  region.setAttribute("aria-atomic", "true");
  region.setAttribute("data-testid", "change-announcer");
  region.className = "sr-only";
  document.body.append(region);
  return region;
}

/** Say `message` once; the same message twice in a row is said again. */
export function announceChange(message: string): void {
  const target = liveRegion();
  if (!target) return;
  target.textContent = "";
  // A fresh text node after clearing makes readers repeat an unchanged message.
  queueMicrotask(() => {
    target.textContent = message;
  });
}
