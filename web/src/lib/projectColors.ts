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

/**
 * A project's mark: an emoji, or a lucide icon and/or a palette color that
 * tints it. An emoji wins over both.
 */
export interface ProjectAppearance {
  icon: string | null;
  /** A lucide-react icon name (e.g. "FlaskConical"); see lib/lucideIconCatalog. */
  lucideIcon: string | null;
  color: string | null;
}

/** An unknown color reads as unset. */
export function projectAppearance(config: ProjectConfig | undefined): ProjectAppearance {
  const icon = config?.icon || null;
  return {
    icon,
    lucideIcon: (!icon && config?.lucide_icon) || null,
    color: !icon && isThemePalette(config?.color) ? config.color : null,
  };
}

/** Store a mark on a config; an emoji clears the lucide icon and color. */
export function withProjectAppearance(
  config: ProjectConfig,
  appearance: ProjectAppearance,
): ProjectConfig {
  const next = { ...config };
  delete next.icon;
  delete next.lucide_icon;
  delete next.color;
  if (appearance.icon) {
    next.icon = appearance.icon;
    return next;
  }
  if (appearance.lucideIcon) next.lucide_icon = appearance.lucideIcon;
  if (appearance.color) next.color = appearance.color;
  return next;
}

export function sameProjectAppearance(a: ProjectAppearance, b: ProjectAppearance): boolean {
  return a.icon === b.icon && a.lucideIcon === b.lucideIcon && a.color === b.color;
}
