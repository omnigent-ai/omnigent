// Terminal colors for each color theme. The terminal is an xterm.js canvas
// themed by a JS object, so it can't follow the CSS tokens the rest of the app
// is skinned with; every palette carries its own full 16-color ANSI table here.
//
// Built-in palettes use their upstream terminal colors (Dracula/Alucard spec,
// GitHub Primer, Catppuccin Latte/Mocha, Gruvbox, Solarized, Nord), with three
// deliberate deviations:
//   - Light variants keep the readability guard: CLIs that assume a dark
//     terminal paint primary text with ANSI white / bright-white, so in light
//     variants those slots are dark text tones instead of near-background
//     shades.
//   - Nord has no upstream light variant and the app's Dracula light is not
//     Alucard, so those light canvases take the app's own light background.
//   - Selection uses the app's translucent selection wash rather than the
//     upstream opaque selection color, which xterm would fade to 30% and which
//     on several light palettes is nearly the canvas color.

import {
  type CustomTheme,
  DEFAULT_CUSTOM_THEME,
  deriveCustomTheme,
  isHexColor,
  rebaseColor,
} from "./customTheme";
import {
  DEFAULT_PALETTE,
  PALETTES,
  type PaletteTokens,
  type ThemePalette,
  type ThemeSelection,
} from "./themePalette";

export interface TerminalColors {
  background: string;
  foreground: string;
  cursor: string;
  cursorAccent: string;
  selectionBackground: string;
  black: string;
  red: string;
  green: string;
  yellow: string;
  blue: string;
  magenta: string;
  cyan: string;
  white: string;
  brightBlack: string;
  brightRed: string;
  brightGreen: string;
  brightYellow: string;
  brightBlue: string;
  brightMagenta: string;
  brightCyan: string;
  brightWhite: string;
}

export interface TerminalPalette {
  light: TerminalColors;
  dark: TerminalColors;
}

type UpstreamColors = Omit<TerminalColors, "cursorAccent" | "selectionBackground">;

// xterm.js's built-in ANSI table (Tango). The Omnigent palette predates
// per-theme terminal colors and only ever overrode a few slots, so it pins
// these to keep the default look unchanged.
const XTERM_DEFAULT_ANSI = {
  red: "#cc0000",
  green: "#4e9a06",
  yellow: "#c4a000",
  blue: "#3465a4",
  magenta: "#75507b",
  cyan: "#06989a",
  brightRed: "#ef2929",
  brightGreen: "#8ae234",
  brightYellow: "#fce94f",
  brightBlue: "#729fcf",
  brightMagenta: "#ad7fa8",
  brightCyan: "#34e2e2",
};

const OMNI_LIGHT_BACKGROUND = "#ffffff";
const OMNI_DARK_BACKGROUND = "#131517";

const OMNI: TerminalPalette = {
  light: {
    ...XTERM_DEFAULT_ANSI,
    background: OMNI_LIGHT_BACKGROUND,
    foreground: "#18181b",
    cursor: "#0891b2",
    cursorAccent: OMNI_LIGHT_BACKGROUND,
    selectionBackground: "#0891b233",
    black: "#18181b",
    brightBlack: "#e4e4e7",
    white: "#3f3f46",
    brightWhite: "#18181b",
  },
  dark: {
    ...XTERM_DEFAULT_ANSI,
    background: OMNI_DARK_BACKGROUND,
    foreground: "#e4e4e7",
    cursor: "#22d3ee",
    cursorAccent: OMNI_DARK_BACKGROUND,
    selectionBackground: "#22d3ee33",
    black: "#09090b",
    brightBlack: "#71717a",
    white: "#d3d7cf",
    brightWhite: "#eeeeec",
  },
};

type UpstreamPalette = Exclude<ThemePalette, "omni">;

const UPSTREAM: Record<UpstreamPalette, { light: UpstreamColors; dark: UpstreamColors }> = {
  dracula: {
    light: {
      background: "#f7f5fd",
      foreground: "#1f1f1f",
      cursor: "#1f1f1f",
      black: "#fffbeb",
      red: "#cb3a2a",
      green: "#14710a",
      yellow: "#846e15",
      blue: "#644ac9",
      magenta: "#a3144d",
      cyan: "#036a96",
      white: "#1f1f1f",
      brightBlack: "#6c664b",
      brightRed: "#d74c3d",
      brightGreen: "#198d0c",
      brightYellow: "#9e841a",
      brightBlue: "#7862d0",
      brightMagenta: "#bf185a",
      brightCyan: "#047fb4",
      brightWhite: "#2c2b31",
    },
    dark: {
      background: "#282a36",
      foreground: "#f8f8f2",
      cursor: "#f8f8f2",
      black: "#21222c",
      red: "#ff5555",
      green: "#50fa7b",
      yellow: "#f1fa8c",
      blue: "#bd93f9",
      magenta: "#ff79c6",
      cyan: "#8be9fd",
      white: "#f8f8f2",
      brightBlack: "#6272a4",
      brightRed: "#ff6e6e",
      brightGreen: "#69ff94",
      brightYellow: "#ffffa5",
      brightBlue: "#d6acff",
      brightMagenta: "#ff92df",
      brightCyan: "#a4ffff",
      brightWhite: "#ffffff",
    },
  },
  github: {
    light: {
      background: "#ffffff",
      foreground: "#1f2328",
      cursor: "#0969da",
      black: "#24292f",
      red: "#cf222e",
      green: "#116329",
      yellow: "#4d2d00",
      blue: "#0969da",
      magenta: "#8250df",
      cyan: "#1b7c83",
      white: "#6e7781",
      brightBlack: "#57606a",
      brightRed: "#a40e26",
      brightGreen: "#1a7f37",
      brightYellow: "#633c01",
      brightBlue: "#218bff",
      brightMagenta: "#a475f9",
      brightCyan: "#3192aa",
      brightWhite: "#1f2328",
    },
    dark: {
      background: "#0d1117",
      foreground: "#e6edf3",
      cursor: "#2f81f7",
      black: "#484f58",
      red: "#ff7b72",
      green: "#3fb950",
      yellow: "#d29922",
      blue: "#58a6ff",
      magenta: "#bc8cff",
      cyan: "#39c5cf",
      white: "#b1bac4",
      brightBlack: "#6e7681",
      brightRed: "#ffa198",
      brightGreen: "#56d364",
      brightYellow: "#e3b341",
      brightBlue: "#79c0ff",
      brightMagenta: "#d2a8ff",
      brightCyan: "#56d4dd",
      brightWhite: "#ffffff",
    },
  },
  catppuccin: {
    light: {
      background: "#eff1f5",
      foreground: "#4c4f69",
      cursor: "#dc8a78",
      black: "#5c5f77",
      red: "#d20f39",
      green: "#40a02b",
      yellow: "#df8e1d",
      blue: "#1e66f5",
      magenta: "#ea76cb",
      cyan: "#179299",
      white: "#5c5f77",
      brightBlack: "#6c6f85",
      brightRed: "#de293e",
      brightGreen: "#49af3d",
      brightYellow: "#eea02d",
      brightBlue: "#456eff",
      brightMagenta: "#fe85d8",
      brightCyan: "#2d9fa8",
      brightWhite: "#4c4f69",
    },
    dark: {
      background: "#1e1e2e",
      foreground: "#cdd6f4",
      cursor: "#f5e0dc",
      black: "#45475a",
      red: "#f38ba8",
      green: "#a6e3a1",
      yellow: "#f9e2af",
      blue: "#89b4fa",
      magenta: "#f5c2e7",
      cyan: "#94e2d5",
      white: "#a6adc8",
      brightBlack: "#585b70",
      brightRed: "#f37799",
      brightGreen: "#89d88b",
      brightYellow: "#ebd391",
      brightBlue: "#74a8fc",
      brightMagenta: "#f2aede",
      brightCyan: "#6bd7ca",
      brightWhite: "#bac2de",
    },
  },
  gruvbox: {
    light: {
      background: "#fbf1c7",
      foreground: "#3c3836",
      cursor: "#3c3836",
      black: "#fbf1c7",
      red: "#cc241d",
      green: "#98971a",
      yellow: "#d79921",
      blue: "#458588",
      magenta: "#b16286",
      cyan: "#689d6a",
      white: "#7c6f64",
      brightBlack: "#928374",
      brightRed: "#9d0006",
      brightGreen: "#79740e",
      brightYellow: "#b57614",
      brightBlue: "#076678",
      brightMagenta: "#8f3f71",
      brightCyan: "#427b58",
      brightWhite: "#3c3836",
    },
    dark: {
      background: "#282828",
      foreground: "#ebdbb2",
      cursor: "#ebdbb2",
      black: "#282828",
      red: "#cc241d",
      green: "#98971a",
      yellow: "#d79921",
      blue: "#458588",
      magenta: "#b16286",
      cyan: "#689d6a",
      white: "#a89984",
      brightBlack: "#928374",
      brightRed: "#fb4934",
      brightGreen: "#b8bb26",
      brightYellow: "#fabd2f",
      brightBlue: "#83a598",
      brightMagenta: "#d3869b",
      brightCyan: "#8ec07c",
      brightWhite: "#ebdbb2",
    },
  },
  solarized: {
    light: {
      background: "#fdf6e3",
      foreground: "#657b83",
      cursor: "#657b83",
      black: "#073642",
      red: "#dc322f",
      green: "#859900",
      yellow: "#b58900",
      blue: "#268bd2",
      magenta: "#d33682",
      cyan: "#2aa198",
      white: "#657b83",
      brightBlack: "#002b36",
      brightRed: "#cb4b16",
      brightGreen: "#586e75",
      brightYellow: "#657b83",
      brightBlue: "#839496",
      brightMagenta: "#6c71c4",
      brightCyan: "#93a1a1",
      brightWhite: "#586e75",
    },
    dark: {
      background: "#002b36",
      foreground: "#839496",
      cursor: "#839496",
      black: "#073642",
      red: "#dc322f",
      green: "#859900",
      yellow: "#b58900",
      blue: "#268bd2",
      magenta: "#d33682",
      cyan: "#2aa198",
      white: "#eee8d5",
      brightBlack: "#002b36",
      brightRed: "#cb4b16",
      brightGreen: "#586e75",
      brightYellow: "#657b83",
      brightBlue: "#839496",
      brightMagenta: "#6c71c4",
      brightCyan: "#93a1a1",
      brightWhite: "#fdf6e3",
    },
  },
  nord: {
    light: {
      background: "#eceff4",
      foreground: "#2e3440",
      cursor: "#2e3440",
      black: "#3b4252",
      red: "#bf616a",
      green: "#96b17f",
      yellow: "#c5a565",
      blue: "#81a1c1",
      magenta: "#b48ead",
      cyan: "#7bb3c3",
      white: "#4c566a",
      brightBlack: "#4c566a",
      brightRed: "#bf616a",
      brightGreen: "#96b17f",
      brightYellow: "#c5a565",
      brightBlue: "#81a1c1",
      brightMagenta: "#b48ead",
      brightCyan: "#82afae",
      brightWhite: "#2e3440",
    },
    dark: {
      background: "#2e3440",
      foreground: "#d8dee9",
      cursor: "#d8dee9",
      black: "#3b4252",
      red: "#bf616a",
      green: "#a3be8c",
      yellow: "#ebcb8b",
      blue: "#81a1c1",
      magenta: "#b48ead",
      cyan: "#88c0d0",
      white: "#e5e9f0",
      brightBlack: "#4c566a",
      brightRed: "#bf616a",
      brightGreen: "#a3be8c",
      brightYellow: "#ebcb8b",
      brightBlue: "#81a1c1",
      brightMagenta: "#b48ead",
      brightCyan: "#8fbcbb",
      brightWhite: "#eceff4",
    },
  },
};

function appTokens(palette: ThemePalette) {
  return (PALETTES.find((candidate) => candidate.id === palette) ?? PALETTES[0]).tokens;
}

// A terminal selection matches a chat-text selection under the same theme.
function fromUpstream(palette: UpstreamPalette): TerminalPalette {
  const variant = (mode: "light" | "dark"): TerminalColors => {
    const colors = UPSTREAM[palette][mode];
    return {
      ...colors,
      cursorAccent: colors.background,
      selectionBackground: appTokens(palette)[mode].selectionBackground,
    };
  };
  return { light: variant("light"), dark: variant("dark") };
}

export const TERMINAL_PALETTES: Record<ThemePalette, TerminalPalette> = {
  omni: OMNI,
  dracula: fromUpstream("dracula"),
  github: fromUpstream("github"),
  catppuccin: fromUpstream("catppuccin"),
  gruvbox: fromUpstream("gruvbox"),
  solarized: fromUpstream("solarized"),
  nord: fromUpstream("nord"),
};

export const DEFAULT_TERMINAL_PALETTE = TERMINAL_PALETTES[DEFAULT_PALETTE];

function customVariant(
  base: TerminalColors,
  appBase: PaletteTokens,
  derived: PaletteTokens,
): TerminalColors {
  // The custom theme only shifts the canvas and text tones; the ANSI table
  // stays the base palette's. Non-hex app tokens (oklch, color-mix) can't be
  // rebased and leave the base terminal color in place.
  const shift = (terminal: string, reference: string, current: string) => {
    const shifted = rebaseColor(terminal, reference, current);
    return isHexColor(shifted) ? shifted : terminal;
  };
  const background = shift(base.background, appBase.background, derived.background);
  return {
    ...base,
    background,
    cursorAccent: background,
    foreground: shift(base.foreground, appBase.foreground, derived.foreground),
    selectionBackground:
      derived.selectionBackground === appBase.selectionBackground
        ? base.selectionBackground
        : derived.selectionBackground,
  };
}

/** Resolve the terminal palette for the app's current color-theme selection. */
export function resolveTerminalPalette(
  selection: ThemeSelection,
  customTheme: CustomTheme = DEFAULT_CUSTOM_THEME,
): TerminalPalette {
  if (selection !== "custom") return TERMINAL_PALETTES[selection] ?? DEFAULT_TERMINAL_PALETTE;
  const base = TERMINAL_PALETTES[customTheme.basePalette] ?? DEFAULT_TERMINAL_PALETTE;
  const tokens = appTokens(customTheme.basePalette);
  const derived = deriveCustomTheme(customTheme);
  return {
    light: customVariant(base.light, tokens.light, derived.light),
    dark: customVariant(base.dark, tokens.dark, derived.dark),
  };
}
