// The Shiki themes the workspace sidebar's code surfaces highlight with: the
// GitHub themes chat colours code with, one per colour scheme.

export type SyntaxThemeMode = "light" | "dark";

/** A light and a dark Shiki theme name, the pair a highlight colours with. */
export type SyntaxThemeNames = Readonly<Record<SyntaxThemeMode, string>>;

/** Theme names, one per mode so Shiki and diffs know which scheme each is. */
export const SYNTAX_THEME_NAMES = {
  light: "github-light",
  dark: "github-dark",
} as const satisfies SyntaxThemeNames;
