import { describe, expect, it } from "vitest";
import {
  DARK_SYNTAX_THEME,
  DARK_SYNTAX_THEME_NAME,
  LIGHT_SYNTAX_THEME,
  LIGHT_SYNTAX_THEME_NAME,
  SYNTAX_THEMES,
} from "./syntaxTheme";

function foregroundFor(theme: typeof LIGHT_SYNTAX_THEME, scope: string): string | undefined {
  const rule = theme.settings.find((entry) => {
    const scopes = Array.isArray(entry.scope) ? entry.scope : [entry.scope];
    return scopes.includes(scope);
  });
  return rule?.settings.foreground;
}

describe("Otto Ink syntax themes", () => {
  it("keeps chat and file-viewer themes in a stable light/dark order", () => {
    expect(SYNTAX_THEMES).toEqual([LIGHT_SYNTAX_THEME, DARK_SYNTAX_THEME]);
    expect(LIGHT_SYNTAX_THEME.name).toBe(LIGHT_SYNTAX_THEME_NAME);
    expect(DARK_SYNTAX_THEME.name).toBe(DARK_SYNTAX_THEME_NAME);
    expect(LIGHT_SYNTAX_THEME.type).toBe("light");
    expect(DARK_SYNTAX_THEME.type).toBe("dark");
  });

  it("uses Omnigent semantic colors consistently across themes", () => {
    expect(foregroundFor(LIGHT_SYNTAX_THEME, "keyword")).toBe("#b72f6e");
    expect(foregroundFor(DARK_SYNTAX_THEME, "keyword")).toBe("#ff7fb5");
    expect(foregroundFor(LIGHT_SYNTAX_THEME, "string")).toBe("#237d63");
    expect(foregroundFor(DARK_SYNTAX_THEME, "string")).toBe("#66cfa9");
    expect(foregroundFor(LIGHT_SYNTAX_THEME, "entity.name.function")).toBe("#176fa6");
    expect(foregroundFor(DARK_SYNTAX_THEME, "entity.name.function")).toBe("#6bb8f0");
  });
});
