import { MAIN_CANVAS_ID } from "./canvasLayout";

export const CANVAS_QUERY_PARAM = "canvas";

/** Match only the Canvas root and session routes under the current mount path. */
export function isCanvasPathname(pathname: string, canvasPath = "/canvas"): boolean {
  return (
    pathname.startsWith(canvasPath) &&
    /^(?:\/c\/[^/]+)?\/?$/.test(pathname.slice(canvasPath.length))
  );
}

export function canvasLocation(canvasId: string, sessionId?: string) {
  return {
    pathname: sessionId ? `/canvas/c/${encodeURIComponent(sessionId)}` : "/canvas",
    search:
      canvasId === MAIN_CANVAS_ID ? "" : `?${CANVAS_QUERY_PARAM}=${encodeURIComponent(canvasId)}`,
  };
}
