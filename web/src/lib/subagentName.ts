// Derive a short, human-readable sub-agent name from a text selection, and keep
// it collision-free within a set of existing names. Used by the "Ask sub-agent"
// flow to prefill the child's name from the highlighted text.

const MAX_NAME_CHARS = 48;
// "first meaningful 5–7 words" for long selections.
const MAX_WORDS = 7;

/**
 * Turn selected chat text into a short display name.
 *
 * Strips control characters and common Markdown syntax, collapses all
 * whitespace (including newlines) to single spaces, and caps the result at 48
 * characters — for long text, the first up-to-7 words that fit, plus an
 * ellipsis. Whitespace/markup-only selections fall back to ``"Sub-agent"``.
 *
 * @param selectedText The exact highlighted text.
 * @returns A name ≤ 48 characters.
 */
export function deriveSubagentName(selectedText: string): string {
  const cleaned = selectedText
    // Remove C0/C1 control characters (keep \t/\n as whitespace — collapsed
    // below); the class is the point of the rule, so allow the control regex.
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "")
    // Drop Markdown syntax so names read as prose.
    .replace(/```[\s\S]*?```/g, " ") // fenced code blocks
    .replace(/`([^`]*)`/g, "$1") // inline code
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links / images → their text
    .replace(/^\s*[-+*]\s+/gm, " ") // list bullets
    .replace(/[*_~`>#|]/g, "") // emphasis / quote / heading / table markers
    .replace(/\s+/g, " ") // collapse all whitespace
    .trim();
  if (cleaned === "") return "Sub-agent";
  if (cleaned.length <= MAX_NAME_CHARS) return cleaned;

  const words = cleaned.split(" ");
  // Take the most words (up to 7) whose join fits within 48 chars incl. "…".
  let count = Math.min(MAX_WORDS, words.length);
  while (count > 1 && words.slice(0, count).join(" ").length + 1 > MAX_NAME_CHARS) {
    count -= 1;
  }
  let base = words.slice(0, count).join(" ");
  // A single very long word still gets hard-capped.
  if (base.length + 1 > MAX_NAME_CHARS) base = base.slice(0, MAX_NAME_CHARS - 1).trimEnd();
  return `${base}…`;
}

/**
 * Disambiguate ``base`` against ``taken`` by appending ``" (2)"``, ``" (3)"``,
 * … only when it collides. A label that isn't taken is returned unchanged (no
 * number when labels differ).
 *
 * @param base The desired name, e.g. ``"Snappy"``.
 * @param taken Names already in use (e.g. across the root tree).
 * @returns A collision-free name.
 */
export function uniqueSubagentName(base: string, taken: readonly string[]): string {
  const used = new Set(taken);
  if (!used.has(base)) return base;
  for (let i = 2; ; i += 1) {
    const candidate = `${base} (${i})`;
    if (!used.has(candidate)) return candidate;
  }
}
