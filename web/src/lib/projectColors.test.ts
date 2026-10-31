import { describe, expect, it } from "vitest";
import {
  PROJECT_COLORS,
  projectAppearance,
  projectColorValue,
  withProjectAppearance,
} from "./projectColors";
import { PALETTES } from "./themePalette";

describe("projectColors", () => {
  it("offers exactly the theme palettes' accents, per mode", () => {
    expect(PROJECT_COLORS.map((color) => color.id)).toEqual(PALETTES.map((palette) => palette.id));
    expect(projectColorValue("dracula", "dark")).toBe(PALETTES[1].dark.accent);
    expect(projectColorValue("#ff0000", "light")).toBeUndefined();
  });

  it("keeps an emoji or a color, never both", () => {
    expect(projectAppearance({ icon: "🔥", color: "nord" })).toEqual({ icon: "🔥", color: null });
    expect(projectAppearance({ color: "#ff0000" })).toEqual({ icon: null, color: null });
    const config = { host_id: "h1", icon: "🔥" };
    expect(withProjectAppearance(config, { icon: null, color: "nord" })).toEqual({
      host_id: "h1",
      color: "nord",
    });
  });
});
