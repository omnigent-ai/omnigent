/**
 * Browser preview bounds conversion: renderer CSS px (getBoundingClientRect) →
 * window device-independent px (WebContentsView#setBounds). Identical at zoom 1,
 * diverge after Cmd+/Cmd-. Pure math (no Electron imports), so unit-testable.
 */

function normalizeBrowserViewBounds(bounds, zoomFactor, displayScaleFactor) {
  if (!bounds) return bounds;
  const rendererDpr = bounds.devicePixelRatio;
  const displayScale =
    Number.isFinite(displayScaleFactor) && displayScaleFactor > 0 ? displayScaleFactor : null;
  const scale =
    Number.isFinite(rendererDpr) && rendererDpr > 0 && displayScale
      ? rendererDpr / displayScale
      : Number.isFinite(zoomFactor) && zoomFactor > 0
        ? zoomFactor
        : 1;
  return {
    x: Math.round(bounds.x * scale),
    y: Math.round(bounds.y * scale),
    width: Math.round(bounds.width * scale),
    height: Math.round(bounds.height * scale),
  };
}

function createBrowserViewBoundsController({
  getZoomFactor,
  getDisplayScaleFactor = () => null,
  setBounds,
}) {
  let rendererBounds = null;
  const apply = () => {
    if (!rendererBounds) return null;
    const normalized = normalizeBrowserViewBounds(
      rendererBounds,
      getZoomFactor(),
      getDisplayScaleFactor(),
    );
    setBounds(normalized);
    return normalized;
  };
  return {
    setRendererBounds(bounds) {
      rendererBounds = bounds;
      return apply();
    },
    resync() {
      return apply();
    },
    clear() {
      rendererBounds = null;
    },
  };
}

module.exports = { normalizeBrowserViewBounds, createBrowserViewBoundsController };
