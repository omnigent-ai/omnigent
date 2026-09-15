import { sessionLinkParams, withSearch } from "@/lib/sessionLinks";

export const CANVAS_SESSION_PARAM = "session";

/** Select a chat on Canvas, or clear it with null, without carrying old file/terminal state. */
export function canvasSessionHref(sessionId: string | null, search = ""): string {
  const params = sessionLinkParams(search);
  params.delete(CANVAS_SESSION_PARAM);
  if (sessionId !== null) {
    params.set(CANVAS_SESSION_PARAM, sessionId);
    params.set("view", "chat");
  }
  return withSearch("/canvas", params);
}
