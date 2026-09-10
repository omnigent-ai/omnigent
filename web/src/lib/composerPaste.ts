// Shared caret-insertion for composer paste handlers.
//
// A paste that carries both files and plain text used to drop the text: the
// paste handlers only look at `clipboardData.items` for files and call
// `preventDefault`, which silently discards whatever text/plain sat alongside
// them. This inserts that text at the caret so a copied prompt with an image
// still pastes back in full.

/**
 * Replace the selection `[start, end)` in `current` with `text`, inserted
 * verbatim (no padding — a paste should land exactly as copied).
 *
 * Indices are clamped to `[0, current.length]` so a stale selection can't
 * throw or write out of bounds.
 */
export function insertTextAtCaret(
  current: string,
  start: number,
  end: number,
  text: string,
): { next: string; caret: number } {
  const from = Math.min(Math.max(start, 0), current.length);
  const to = Math.min(Math.max(end, 0), current.length);
  const lo = Math.min(from, to);
  const hi = Math.max(from, to);
  return {
    next: current.slice(0, lo) + text + current.slice(hi),
    caret: lo + text.length,
  };
}
