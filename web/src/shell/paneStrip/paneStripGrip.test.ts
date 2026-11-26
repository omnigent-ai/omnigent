import { describe, expect, it } from "vitest";

import {
  PANE_GRIP_BAND_PX,
  PANE_GRIP_MIN_ROW_PX,
  paneStripZone,
  zoneMaximizes,
  zoneMoves,
} from "./paneStripGrip";

describe("paneStripZone", () => {
  const row = { left: 100, width: 600 };
  const centre = 400;

  it("is the band within half its width of the row's centre, edges included", () => {
    expect(paneStripZone(row, centre)).toBe("grip");
    expect(paneStripZone(row, centre - PANE_GRIP_BAND_PX / 2)).toBe("grip");
    expect(paneStripZone(row, centre + PANE_GRIP_BAND_PX / 2)).toBe("grip");
    expect(paneStripZone(row, centre - PANE_GRIP_BAND_PX / 2 - 1)).toBe("row");
    expect(paneStripZone(row, row.left + 1)).toBe("row");
  });

  it("is the whole row below the minimum width, which has no room for both zones", () => {
    expect(paneStripZone({ left: 0, width: PANE_GRIP_MIN_ROW_PX - 1 }, 5)).toBe("whole");
    expect(paneStripZone({ left: 0, width: PANE_GRIP_MIN_ROW_PX }, 5)).toBe("row");
  });

  it("moves from the band or a narrow row, and maximizes from the rest or a narrow row", () => {
    expect([zoneMoves("grip"), zoneMoves("row"), zoneMoves("whole")]).toEqual([true, false, true]);
    expect([zoneMaximizes("grip"), zoneMaximizes("row"), zoneMaximizes("whole")]).toEqual([
      false,
      true,
      true,
    ]);
  });
});
