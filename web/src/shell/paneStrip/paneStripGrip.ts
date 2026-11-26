// Where on a movable pane's toolbar a press lands: the band around its move handle, which moves
// the pane, or the rest of the blank space, where a double-click maximizes it. A toolbar too
// narrow for both moves from anywhere, and a double-click anywhere maximizes.

/** The band around the handle, centred on the toolbar, that moves the pane. */
export const PANE_GRIP_BAND_PX = 120;
/** The narrowest toolbar that splits its blank space into the band and the rest. */
export const PANE_GRIP_MIN_ROW_PX = PANE_GRIP_BAND_PX * 3;

/** `grip`: the band around the handle. `row`: the rest. `whole`: a narrow row, both at once. */
export type PaneStripZone = "grip" | "row" | "whole";

/** The zone of a toolbar spanning `row` that a pointer at `clientX` is over. */
export function paneStripZone(
  row: { left: number; width: number },
  clientX: number,
): PaneStripZone {
  if (row.width < PANE_GRIP_MIN_ROW_PX) return "whole";
  const centre = row.left + row.width / 2;
  return Math.abs(clientX - centre) <= PANE_GRIP_BAND_PX / 2 ? "grip" : "row";
}

/** Whether a press in `zone` moves the pane. */
export const zoneMoves = (zone: PaneStripZone): boolean => zone !== "row";

/** Whether a double-click in `zone` maximizes or restores the pane. */
export const zoneMaximizes = (zone: PaneStripZone): boolean => zone !== "grip";
