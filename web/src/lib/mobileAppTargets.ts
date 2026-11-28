import { getStyleRoot } from "./host";
import { isAndroidShell, isIOSShell } from "./nativeBridge";

/** The style root also contains portals; the embed must not mark its host document. */
export function applyMobileAppTargetAttribute(): void {
  const root = getStyleRoot();
  if (!root) return;
  if (isIOSShell() || isAndroidShell()) root.dataset.mobileApp = "true";
  else delete root.dataset.mobileApp;
}
