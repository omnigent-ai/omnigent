import { describe, expect, it } from "vitest";
import { updateOverlayToastOffset } from "./updateOverlayInset";

describe("updateOverlayToastOffset", () => {
  it("uses only the normal safe-area offset while the overlay is hidden", () => {
    expect(updateOverlayToastOffset(0)).toBe("calc(1rem + var(--omnigent-inset-bottom) + 0px)");
  });

  it("reserves the measured card height plus the shell gap", () => {
    expect(updateOverlayToastOffset(180.4)).toBe(
      "calc(1rem + var(--omnigent-inset-bottom) + 192px)",
    );
  });

  it("clamps invalid and negative heights", () => {
    expect(updateOverlayToastOffset(Number.NaN)).toContain("+ 0px");
    expect(updateOverlayToastOffset(-20)).toContain("+ 0px");
  });
});
