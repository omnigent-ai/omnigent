import type { CSSProperties } from "react";

// Terminal colors in each scheme, shared by the xterm pane and the chat's
// terminal-styled shell blocks so both read as the same terminal. The
// backgrounds match the app's default `--card`.
const TERMINAL_COLORS = {
  light: {
    background: "#ffffff",
    foreground: "#18181b",
    muted: "#f4f4f5",
    mutedForeground: "#71717a",
    border: "#e4e4e7",
    destructive: "#c8324c",
  },
  dark: {
    background: "#131517",
    foreground: "#e4e4e7",
    muted: "#27272a",
    mutedForeground: "#a1a1aa",
    border: "#27272a",
    destructive: "#e65b77",
  },
} as const;

/** The terminal's background and text colors in the given scheme. */
export function terminalSurfaceColors(isDark: boolean): { background: string; foreground: string } {
  return isDark ? TERMINAL_COLORS.dark : TERMINAL_COLORS.light;
}

/**
 * Style that repaints a DOM block in the terminal's scheme by overriding the
 * theme variables its utilities read, for when the terminal theme setting
 * differs from the app's.
 */
export function terminalSurfaceStyle(isDark: boolean): CSSProperties {
  const colors = isDark ? TERMINAL_COLORS.dark : TERMINAL_COLORS.light;
  return {
    colorScheme: isDark ? "dark" : "light",
    "--background": colors.background,
    "--foreground": colors.foreground,
    "--muted": colors.muted,
    "--muted-foreground": colors.mutedForeground,
    "--border": colors.border,
    "--destructive": colors.destructive,
  } as CSSProperties;
}
