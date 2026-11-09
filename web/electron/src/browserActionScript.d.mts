export function buildBrowserActionScript(
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
): string;

/** @deprecated Remove in v0.21 with the desktop/UI transition adapters. */
export function buildLegacyBrowserActionScript(
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
): string;
