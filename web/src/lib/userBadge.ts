// Deterministic per-user avatar styling (initials + color) derived
// from the user's email. The server only knows email identities, so
// the same email always renders the same circle everywhere it appears
// (presence stack, message attribution) and across sessions/devices —
// no profile store needed.

/**
 * Initials for a user's avatar circle, from the email local part.
 * `"alice.smith@x.com"` → `"AS"`, `"bob@x.com"` → `"B"`.
 */
export function userInitials(userId: string): string {
  const localPart = userId.split("@", 1)[0] ?? userId;
  const segments = localPart.split(/[._\-+]+/).filter(Boolean);
  if (segments.length === 0) return localPart.slice(0, 1).toUpperCase();
  if (segments.length === 1) return segments[0]!.slice(0, 1).toUpperCase();
  return (segments[0]!.slice(0, 1) + segments[1]!.slice(0, 1)).toUpperCase();
}

// Per-user accent slots drawn from the app's own design tokens (see
// `src/index.css`): the categorical chart colors plus the brand
// accent, each with light/dark-theme variants the CSS vars resolve
// automatically. `--chart-5` is excluded — it's the palette's grey,
// which would be indistinguishable from the idle (desaturated)
// presence state.
const USER_PALETTE = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--brand-accent)",
] as const;

/** FNV-1a hash of the identity, for stable palette-slot assignment. */
function userHash(userId: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < userId.length; i += 1) {
    hash ^= userId.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

/**
 * Stable per-user accent color as a theme-palette CSS token. The same
 * email always resolves to the same slot; distinct users can share a
 * slot once more than five are involved (a deliberate trade for
 * staying inside the UI's palette).
 */
export function userColor(userId: string): string {
  return USER_PALETTE[userHash(userId) % USER_PALETTE.length]!;
}

/**
 * The same per-user token at low alpha (via `color-mix`), for tinting
 * a message bubble's background — authorship reads at a glance while
 * the text keeps normal-foreground contrast in both themes.
 */
export function userColorTint(userId: string): string {
  return `color-mix(in oklab, ${userColor(userId)} 15%, transparent)`;
}
