const STORAGE_KEY = "omnigent:sidebar-font-size";

export const SIDEBAR_FONT_SIZE_DEFAULT = 13;
export const SIDEBAR_FONT_SIZE_MIN = 11;
export const SIDEBAR_FONT_SIZE_MAX = 16;
export const SIDEBAR_FONT_SIZE_STEP = 1;

export function clampSidebarFontSizePx(px: number): number {
  return Math.min(SIDEBAR_FONT_SIZE_MAX, Math.max(SIDEBAR_FONT_SIZE_MIN, Math.round(px)));
}

function isValidPx(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function readSidebarFontSizePx(): number {
  if (typeof window === "undefined") return SIDEBAR_FONT_SIZE_DEFAULT;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return SIDEBAR_FONT_SIZE_DEFAULT;
    const parsed: unknown = JSON.parse(raw);
    if (!isValidPx(parsed)) return SIDEBAR_FONT_SIZE_DEFAULT;
    return clampSidebarFontSizePx(parsed);
  } catch {
    return SIDEBAR_FONT_SIZE_DEFAULT;
  }
}

export function writeSidebarFontSizePx(px: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(clampSidebarFontSizePx(px)));
  } catch {
    // localStorage quota or access errors should not break settings.
  }
}

export function applySidebarFontSize(px: number): void {
  if (typeof document === "undefined") return;
  document.documentElement.style.setProperty(
    "--sidebar-font-size",
    `${clampSidebarFontSizePx(px)}px`,
  );
}
