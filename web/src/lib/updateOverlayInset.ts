export function updateOverlayToastOffset(height: number): string {
  const normalized = Number.isFinite(height) ? Math.max(0, Math.round(height)) : 0;
  const overlayInset = normalized > 0 ? normalized + 12 : 0;
  return `calc(1rem + var(--omnigent-inset-bottom) + ${overlayInset}px)`;
}

/** Toaster offsets: top clears the native status bar / Dynamic Island, bottom the update overlay. */
export function toasterOffsets(updateOverlayHeight: number) {
  const top = "calc(1rem + var(--omnigent-safe-top))";
  const bottom = updateOverlayToastOffset(updateOverlayHeight);
  return {
    offset: { top, right: "1rem", bottom },
    mobileOffset: { top, right: "1rem", bottom },
  };
}
