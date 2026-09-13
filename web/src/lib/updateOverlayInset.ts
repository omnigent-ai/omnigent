export function updateOverlayToastOffset(height: number): string {
  const normalized = Number.isFinite(height) ? Math.max(0, Math.round(height)) : 0;
  const overlayInset = normalized > 0 ? normalized + 12 : 0;
  return `calc(1rem + var(--omnigent-inset-bottom) + ${overlayInset}px)`;
}
