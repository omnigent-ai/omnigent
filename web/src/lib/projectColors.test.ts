import { describe, expect, it } from "vitest";
import {
  PROJECT_COLORS,
  projectAppearance,
  projectColorValue,
  sameProjectAppearance,
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
    expect(projectAppearance({ icon: "🔥", color: "nord" })).toEqual({
      icon: "🔥",
      lucideIcon: null,
      color: null,
    });
    expect(projectAppearance({ color: "#ff0000" })).toEqual({
      icon: null,
      lucideIcon: null,
      color: null,
    });
    const config = { host_id: "h1", icon: "🔥" };
    expect(withProjectAppearance(config, { icon: null, lucideIcon: null, color: "nord" })).toEqual({
      host_id: "h1",
      color: "nord",
    });
  });

  it("keeps a lucide icon beside a color, and an emoji replaces both", () => {
    expect(projectAppearance({ lucide_icon: "FlaskConical", color: "nord" })).toEqual({
      icon: null,
      lucideIcon: "FlaskConical",
      color: "nord",
    });
    expect(projectAppearance({ icon: "🔥", lucide_icon: "FlaskConical" })).toEqual({
      icon: "🔥",
      lucideIcon: null,
      color: null,
    });
    const iconConfig = { host_id: "h1", icon: "🔥" };
    expect(
      withProjectAppearance(iconConfig, { icon: null, lucideIcon: "FlaskConical", color: "nord" }),
    ).toEqual({ host_id: "h1", lucide_icon: "FlaskConical", color: "nord" });
    const lucideConfig = { lucide_icon: "FlaskConical", color: "nord" };
    expect(
      withProjectAppearance(lucideConfig, {
        icon: "🔥",
        lucideIcon: "FlaskConical",
        color: "nord",
      }),
    ).toEqual({ icon: "🔥" });
  });

  it("compares every part of a mark", () => {
    const mark = { icon: null, lucideIcon: "FlaskConical", color: "nord" };
    expect(sameProjectAppearance(mark, { ...mark })).toBe(true);
    expect(sameProjectAppearance(mark, { ...mark, lucideIcon: "Rocket" })).toBe(false);
    expect(sameProjectAppearance(mark, { ...mark, color: "dracula" })).toBe(false);
  });
});
