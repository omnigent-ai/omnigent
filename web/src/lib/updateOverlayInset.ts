export function updateOverlayToastOffset(height: number): string {
  const normalized = Number.isFinite(height) ? Math.max(0, Math.round(height)) : 0;
  const overlayInset = normalized > 0 ? normalized + 12 : 0;
  return `calc(1rem + var(--omnigent-inset-bottom) + ${overlayInset}px)`;
}

/** Toaster offsets: top clears the native status bar / Dynamic Island, bottom the update overlay. */
export function toasterOffsets(updateOverlayHeight: number) {
  const bottom = updateOverlayToastOffset(updateOverlayHeight);
  return {
    offset: { top: "calc(24px + var(--omnigent-inset-top))", right: "1rem", bottom },
    mobileOffset: { top: "calc(16px + var(--omnigent-inset-top))", right: "1rem", bottom },
  };
}
