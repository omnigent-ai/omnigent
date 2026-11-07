import { describe, expect, it } from "vitest";
import { toasterOffsets, updateOverlayToastOffset } from "./updateOverlayInset";

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

describe("toasterOffsets", () => {
  it("keeps top toasts below the native status bar inset", () => {
    const { offset, mobileOffset } = toasterOffsets(180.4);
    expect(offset.top).toBe("calc(24px + var(--omnigent-inset-top))");
    expect(mobileOffset.top).toBe("calc(16px + var(--omnigent-inset-top))");
    expect(offset.bottom).toBe(updateOverlayToastOffset(180.4));
    expect(mobileOffset.bottom).toBe(updateOverlayToastOffset(180.4));
  });
});
