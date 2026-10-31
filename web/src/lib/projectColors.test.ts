import { describe, expect, it } from "vitest";
import {
  PROJECT_COLORS,
  isProjectColor,
  projectAppearance,
  projectColorValue,
  withProjectAppearance,
} from "./projectColors";
import { PALETTES } from "./themePalette";

describe("PROJECT_COLORS", () => {
  it("reuses each theme palette's accent swatches and adds no others", () => {
    expect(PROJECT_COLORS).toEqual(
      PALETTES.map((palette) => ({
        id: palette.id,
        label: palette.label,
        light: palette.light.accent,
        dark: palette.dark.accent,
      })),
    );
  });
});

describe("projectColorValue", () => {
  it("resolves a palette id to its accent for the active mode", () => {
    expect(projectColorValue("dracula", "light")).toBe("#7c3aed");
    expect(projectColorValue("dracula", "dark")).toBe("#bd93f9");
  });

  it("returns undefined for unset or unknown colors", () => {
    expect(projectColorValue(undefined, "light")).toBeUndefined();
    expect(projectColorValue(null, "dark")).toBeUndefined();
    expect(projectColorValue("#ff0000", "light")).toBeUndefined();
    expect(isProjectColor("#ff0000")).toBe(false);
  });
});

describe("projectAppearance", () => {
  it("lets an emoji win over a stored color", () => {
    expect(projectAppearance({ icon: "🔥", color: "nord" })).toEqual({ icon: "🔥", color: null });
  });

  it("reads a palette color, and an unknown or missing mark as unset", () => {
    expect(projectAppearance({ color: "nord" })).toEqual({ icon: null, color: "nord" });
    expect(projectAppearance({ color: "#ff0000" })).toEqual({ icon: null, color: null });
    expect(projectAppearance(undefined)).toEqual({ icon: null, color: null });
  });
});

describe("withProjectAppearance", () => {
  const base = { host_id: "h1", icon: "🔥", color: "nord" };

  it("keeps only the emoji when an emoji is picked", () => {
    expect(withProjectAppearance(base, { icon: "🚀", color: null })).toEqual({
      host_id: "h1",
      icon: "🚀",
    });
  });

  it("keeps only the color when a color is picked", () => {
    expect(withProjectAppearance(base, { icon: null, color: "github" })).toEqual({
      host_id: "h1",
      color: "github",
    });
  });

  it("clears both when neither is set, leaving other keys", () => {
    expect(withProjectAppearance(base, { icon: null, color: null })).toEqual({ host_id: "h1" });
  });
});
