export function buildBrowserActionScript(
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
): string;

export function jsString(s: string): string;
export function jsNumber(n: number): string;
/** In-page JS that sets `el` from a `ref` (+ `snapshot_id`) or `selector`, or throws. */
export function findElJs(args: Record<string, unknown>): string;
/** The snapshot script as v0.18's relay sent it; desktops of that era accept only this one. */
export const LEGACY_SNAPSHOT_JS: string;
