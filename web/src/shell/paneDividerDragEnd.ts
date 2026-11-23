// Why a divider or corner drag ended without saving its sizes, logged so a report of a resize
// that snapped back names the cause. The line carries the session id, the handle and the reason
// only; the renderer-console capture saves it.

/** The pointer was taken away from the handle before its release. */
export type PointerDragEnd = "pointer-cancelled" | "pointer-lost";

/** The layout ended the drag: it stopped showing the split, a pane, or the held divider. */
export type LayoutDragEnd = "split-hidden" | "pane-maximized" | "divider-gone" | "panes-unmounted";

export type DividerDragEnd = PointerDragEnd | LayoutDragEnd;

/** Characters a session id may keep in the line; anything else is masked. */
const LOGGABLE_ID = /^[A-Za-z0-9_.:-]{1,128}$/;

/** The one console line for a drag that ended unsaved. */
export function formatDividerDragEnd(
  conversationId: string,
  handle: "divider" | "corner",
  reason: DividerDragEnd,
): string {
  const session = LOGGABLE_ID.test(conversationId) ? conversationId : "invalid";
  return `[pane-divider] drag ended unsaved session=${session} handle=${handle} reason=${reason}`;
}

/** Log a drag that ended unsaved. Never throws: a failing console must not break the panes. */
export function reportDividerDragEnd(
  conversationId: string,
  handle: "divider" | "corner",
  reason: DividerDragEnd,
): void {
  try {
    console.info(formatDividerDragEnd(conversationId, handle, reason));
  } catch {
    // Diagnostics are best-effort; the panes behave the same without them.
  }
}
