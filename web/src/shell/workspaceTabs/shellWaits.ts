// Each shell tab's current wait to connect, kept by the session's tab store rather than a panel,
// so a panel that replaces another keeps timing it. A wait for something new starts over.

export interface ShellWaits {
  /**
   * Take in what each shell waits for now, by shell key; a shell left out isn't waiting.
   * Reports whether any wait started, started over for something new, or ended.
   */
  track: (waiting: ReadonlyMap<string, string>) => boolean;
  /** When ``key``'s current wait began, or undefined while it isn't waiting. */
  since: (key: string) => number | undefined;
  /** Start every current wait over from now, as Try Again and Resume do. */
  restart: () => void;
}

export function createShellWaits(): ShellWaits {
  const waits = new Map<string, { reason: string; since: number }>();
  return {
    track(waiting) {
      let changed = false;
      for (const key of waits.keys()) {
        if (waiting.has(key)) continue;
        waits.delete(key);
        changed = true;
      }
      for (const [key, reason] of waiting) {
        if (waits.get(key)?.reason === reason) continue;
        waits.set(key, { reason, since: Date.now() });
        changed = true;
      }
      return changed;
    },
    since: (key) => waits.get(key)?.since,
    restart() {
      const now = Date.now();
      for (const wait of waits.values()) wait.since = now;
    },
  };
}
