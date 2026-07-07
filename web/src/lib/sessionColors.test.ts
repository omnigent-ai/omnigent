import { describe, expect, it } from "vitest";
import {
  SESSION_COLOR_LABEL_KEY,
  SESSION_COLORS,
  sessionColorName,
  sessionColorSwatch,
  sessionColorTint,
} from "./sessionColors";

describe("sessionColorName", () => {
  it("returns the color name when the label holds a known palette name", () => {
    expect(sessionColorName({ labels: { [SESSION_COLOR_LABEL_KEY]: "blue" } })).toBe("blue");
  });

  it("returns null when no color label is set", () => {
    expect(sessionColorName({ labels: {} })).toBeNull();
    expect(sessionColorName({})).toBeNull();
    expect(sessionColorName({ labels: null })).toBeNull();
  });

  it("self-heals an unknown/removed color name to null", () => {
    expect(sessionColorName({ labels: { [SESSION_COLOR_LABEL_KEY]: "chartreuse" } })).toBeNull();
    expect(sessionColorName({ labels: { [SESSION_COLOR_LABEL_KEY]: "" } })).toBeNull();
  });
});

describe("sessionColorSwatch", () => {
  it("maps a known name to its CSS token", () => {
    expect(sessionColorSwatch("blue")).toBe("var(--status-blue)");
    expect(sessionColorSwatch("purple")).toBe("var(--status-purple)");
    expect(sessionColorSwatch("pink")).toBe("var(--brand-accent)");
  });

  it("returns undefined for null, an unknown, or a dropped name", () => {
    expect(sessionColorSwatch(null)).toBeUndefined();
    expect(sessionColorSwatch("nope")).toBeUndefined();
    // "gray" was removed from the palette — it must no longer resolve.
    expect(sessionColorSwatch("gray")).toBeUndefined();
  });
});

describe("sessionColorTint", () => {
  it("wraps the token in a low-alpha color-mix for a known name", () => {
    expect(sessionColorTint("green")).toBe(
      "color-mix(in oklab, var(--status-green) 14%, transparent)",
    );
  });

  it("returns undefined for null or an unknown name", () => {
    expect(sessionColorTint(null)).toBeUndefined();
    expect(sessionColorTint("nope")).toBeUndefined();
  });
});

describe("SESSION_COLORS", () => {
  it("has unique names and every token is a CSS var reference", () => {
    const names = SESSION_COLORS.map((c) => c.name);
    expect(new Set(names).size).toBe(names.length);
    for (const c of SESSION_COLORS) expect(c.token).toMatch(/^var\(--[\w-]+\)$/);
  });
});
