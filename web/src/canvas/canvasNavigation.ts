import { MAIN_CANVAS_ID } from "./canvasLayout";

export const CANVAS_QUERY_PARAM = "canvas";

/** Matches Canvas in standalone and embedded routes; extensions are checked by the caller. */
export function isCanvasPathname(pathname: string): boolean {
  return /\/canvas(?:\/c\/[^/]+)?\/?$/.test(pathname);
}

export function canvasLocation(canvasId: string, sessionId?: string) {
  return {
    pathname: sessionId ? `/canvas/c/${encodeURIComponent(sessionId)}` : "/canvas",
    search:
      canvasId === MAIN_CANVAS_ID ? "" : `?${CANVAS_QUERY_PARAM}=${encodeURIComponent(canvasId)}`,
  };
}
