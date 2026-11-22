// Each shell tab's current wait to connect, kept by the session's tab store rather than a panel,
// so a panel that replaces another keeps timing it. A wait for something new starts over.

export interface ShellWaits {
  /** Take in what each shell waits for now, by shell key; a shell left out isn't waiting. */
  track: (waiting: ReadonlyMap<string, string>) => void;
  /**
   * When ``key``'s current wait began, or undefined while it isn't waiting. With ``reason``,
   * undefined unless that is what it waits for, as a wait for something new starts over.
   */
  since: (key: string, reason?: string) => number | undefined;
  /** Start every current wait over from now, as Try Again and Resume do. */
  restart: () => void;
}

export function createShellWaits(): ShellWaits {
  const waits = new Map<string, { reason: string; since: number }>();
  return {
    track(waiting) {
      for (const key of waits.keys()) if (!waiting.has(key)) waits.delete(key);
      for (const [key, reason] of waiting) {
        if (waits.get(key)?.reason !== reason) waits.set(key, { reason, since: Date.now() });
      }
    },
    since: (key, reason) => {
      const wait = waits.get(key);
      return reason === undefined || wait?.reason === reason ? wait?.since : undefined;
    },
    restart() {
      const now = Date.now();
      for (const wait of waits.values()) wait.since = now;
    },
  };
}
