// Shared "which shell to launch" logic for the workspace rail's "+" menu
// (``NewTabMenu``) and the new-shell hotkey (``useNewShellHotkey``), so both
// agree on the remembered default and persist picks the same way.

// localStorage key for the last shell type launched. App-global (not
// per-session): the user's preferred shell rarely varies by conversation, and
// the "+" menu renders in two strip spots that must agree on the current pick.
export const PREFERRED_SHELL_KEY = "omnigent:preferred-shell";

/** The remembered shell type, or null if unset / storage unavailable. */
export function readPreferredShell(): string | null {
  try {
    return window.localStorage.getItem(PREFERRED_SHELL_KEY);
  } catch {
    return null;
  }
}

/** Persist ``name`` as the preferred shell type; a no-op if storage is unavailable. */
export function writePreferredShell(name: string): void {
  try {
    window.localStorage.setItem(PREFERRED_SHELL_KEY, name);
  } catch {
    /* storage unavailable — the in-memory pick still holds for this mount */
  }
}

/**
 * The shell type to launch by default from ``declared`` terminals: the
 * remembered pick when it is still a declared type, else the first declared
 * name. Returns null when nothing is declared (no shell to launch).
 *
 * @param declared Terminal names from the agent spec, in declaration order.
 * @param preferred The remembered pick (defaults to reading localStorage).
 */
export function resolveDefaultShell(
  declared: readonly string[],
  preferred: string | null = readPreferredShell(),
): string | null {
  if (declared.length === 0) return null;
  return preferred !== null && declared.includes(preferred) ? preferred : declared[0];
}
