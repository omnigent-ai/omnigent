import { describe, expect, it } from "vitest";
import { selectionColors } from "./selectionColors";

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((offset) => {
    const value = Number.parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrastRatio(first: string, second: string): number {
  const values = [luminance(first), luminance(second)];
  return (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05);
}

describe("selectionColors", () => {
  it("preserves a contrasting accent exactly", () => {
    expect(selectionColors("#2563EB", ["#ffffff", "#eeeeee"])).toEqual({
      selectionBackground: "#2563EB",
      selectionForeground: "#ffffff",
    });
  });

  it("adjusts only as far as the adjacent passing 8-bit color", () => {
    expect(contrastRatio("#959595", "#ffffff")).toBeLessThan(3);
    expect(selectionColors("#959595", ["#ffffff"]).selectionBackground).toBe("#949494");
    expect(selectionColors("#949494", ["#ffffff"]).selectionBackground).toBe("#949494");
  });

  it.each(["#000000", "#ffffff", "#777777", "#ffff00", "#ff00ff", "#0000ff", "#00ff00"])(
    "keeps selected text readable and distinguishes %s on light, dark, and middle surfaces",
    (accent) => {
      for (const surfaces of [
        ["#ffffff", "#eeeeee"],
        ["#0e1013", "#181f25", "#404347"],
        ["#777777", "#888888"],
      ]) {
        const result = selectionColors(accent, surfaces);
        expect(
          contrastRatio(result.selectionBackground, result.selectionForeground),
        ).toBeGreaterThanOrEqual(4.5);
        for (const surface of surfaces) {
          expect(contrastRatio(result.selectionBackground, surface)).toBeGreaterThanOrEqual(3);
        }
      }
    },
  );

  it("composites translucent code surfaces onto the background", () => {
    const opaque = selectionColors("#dddddd", ["#ffffff", "#cccccc"]);
    expect(selectionColors("#dddddd", ["#ffffff", "rgba(0, 0, 0, 0.2)"])).toEqual(opaque);
    expect(selectionColors("#dddddd", ["#ffffff", "#00000033"])).toEqual(opaque);
  });

  it("uses the rendered OKLCH code surface", () => {
    const result = selectionColors("#404346", ["#0e1013", "oklch(0.38 0.005 240)"]);
    expect(result.selectionBackground).not.toBe("#404346");
    expect(contrastRatio(result.selectionBackground, "#0e1013")).toBeGreaterThanOrEqual(3);
    // This near-neutral OKLCH color renders close to #404346 in sRGB.
    expect(contrastRatio(result.selectionBackground, "#404346")).toBeGreaterThan(2.95);
  });

  it("includes fixed canvas stops when custom surfaces move away from them", () => {
    const surfaces = ["#777777", "#888888"];
    expect(
      selectionColors("#238636", surfaces, "linear-gradient(160deg, #0d1117 0%, #0a0e14 100%)"),
    ).toEqual(selectionColors("#238636", [...surfaces, "#0d1117", "#0a0e14"]));
    expect(selectionColors("#238636", surfaces, "var(--background)")).toEqual(
      selectionColors("#238636", surfaces),
    );
  });

  it("includes overlapping translucent canvas gradients in their CSS layer order", () => {
    const shell =
      "radial-gradient(rgba(255, 0, 0, 0.2), transparent), radial-gradient(rgba(0, 255, 0, 0.1), transparent), linear-gradient(#000000, #000000)";
    expect(selectionColors("#303030", ["#000000"], shell)).toEqual(
      selectionColors("#303030", [
        "#000000",
        "rgba(51, 0, 0, 1)",
        "rgba(0, 25.5, 0, 1)",
        "rgba(51, 20.4, 0, 1)",
      ]),
    );
  });

  it("maximizes the worst surface contrast when no mix can reach 3:1", () => {
    const surfaces = ["#000000", "#777777", "#ffffff"];
    const result = selectionColors("#777777", surfaces);
    const minimum = (color: string) =>
      Math.min(...surfaces.map((surface) => contrastRatio(color, surface)));
    const score = minimum(result.selectionBackground);
    expect(score).toBeLessThan(3);
    for (let channel = 0; channel <= 255; channel += 1) {
      const gray = `#${channel.toString(16).padStart(2, "0").repeat(3)}`;
      expect(score).toBeGreaterThanOrEqual(minimum(gray));
    }
    expect(
      contrastRatio(result.selectionBackground, result.selectionForeground),
    ).toBeGreaterThanOrEqual(4.5);
  });
});
