// A project stores a theme palette id in `config.color` and renders as that
// palette's accent for the current mode.

import type { ProjectConfig } from "./projectsApi";
import { PALETTES, isThemePalette } from "./themePalette";

export const PROJECT_COLORS = PALETTES.map((palette) => ({
  id: palette.id,
  label: palette.label,
  light: palette.light.accent,
  dark: palette.dark.accent,
}));

export function projectColorValue(
  color: string | null | undefined,
  mode: "light" | "dark",
): string | undefined {
  const match = PROJECT_COLORS.find((option) => option.id === color);
  return match?.[mode];
}

/** A project's mark: an emoji or a palette color, never both (emoji wins). */
export interface ProjectAppearance {
  icon: string | null;
  color: string | null;
}

/** An unknown color reads as unset. */
export function projectAppearance(config: ProjectConfig | undefined): ProjectAppearance {
  const icon = config?.icon || null;
  return { icon, color: !icon && isThemePalette(config?.color) ? config.color : null };
}

/** Store a mark on a config, clearing the other kind so only one is kept. */
export function withProjectAppearance(
  config: ProjectConfig,
  appearance: ProjectAppearance,
): ProjectConfig {
  const next = { ...config };
  delete next.icon;
  delete next.color;
  if (appearance.icon) next.icon = appearance.icon;
  else if (appearance.color) next.color = appearance.color;
  return next;
}
