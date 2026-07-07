// Per-session color — a small curated palette a user can tag a session with,
// shown as a low-alpha tint on the sidebar row. Stored server-side as a
// reserved `omni_color` label (see useConversations), so the color is shared
// and survives reload, mirroring how projects use `omni_project`.
//
// The label VALUE is a palette NAME (e.g. "blue"), not a raw color: names are
// short, validatable, and theme-agnostic — each resolves to a CSS token that
// carries its own light/dark value, and an unknown/removed name self-heals to
// "no color".

/** Reserved conversation-label key holding a session's color name. */
export const SESSION_COLOR_LABEL_KEY = "omni_color";

export interface SessionColor {
  /** Stored label value and test-id suffix, e.g. `"blue"`. */
  name: string;
  /** Human label shown in the picker, e.g. `"Blue"`. */
  label: string;
  /** CSS color token (resolves per theme), e.g. `"var(--status-blue)"`. */
  token: string;
}

/**
 * Curated palette, drawn from the app's hue-stable status tokens (plus the
 * brand pink) so both light and dark themes resolve with no extra CSS.
 */
export const SESSION_COLORS: readonly SessionColor[] = [
  { name: "blue", label: "Blue", token: "var(--status-blue)" },
  { name: "green", label: "Green", token: "var(--status-green)" },
  { name: "yellow", label: "Yellow", token: "var(--status-yellow)" },
  { name: "red", label: "Red", token: "var(--status-red)" },
  { name: "purple", label: "Purple", token: "var(--status-purple)" },
  { name: "pink", label: "Pink", token: "var(--brand-accent)" },
] as const;

const COLORS_BY_NAME = new Map(SESSION_COLORS.map((c) => [c.name, c]));

/**
 * The validated color name set on a conversation, or `null` when unset or the
 * stored value isn't a known palette name (self-healing — a color removed from
 * the palette renders as no color rather than breaking). Structurally typed on
 * `labels` so this module has no dependency on the `Conversation` type.
 */
export function sessionColorName(conversation: {
  labels?: Record<string, string> | null;
}): string | null {
  const raw = conversation.labels?.[SESSION_COLOR_LABEL_KEY];
  return raw && COLORS_BY_NAME.has(raw) ? raw : null;
}

/** Solid CSS token for a color name (for picker swatches), or `undefined`. */
export function sessionColorSwatch(name: string | null): string | undefined {
  return name ? COLORS_BY_NAME.get(name)?.token : undefined;
}

/**
 * A low-alpha tint of the color for a row background, or `undefined` when
 * there's no (valid) color. Mirrors `userColorTint`'s `color-mix` approach so
 * the row text keeps normal-foreground contrast in both themes.
 */
export function sessionColorTint(name: string | null): string | undefined {
  const token = sessionColorSwatch(name);
  return token ? `color-mix(in oklab, ${token} 14%, transparent)` : undefined;
}
