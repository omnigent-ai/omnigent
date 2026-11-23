// How many shell tabs keep their terminal attached while hidden: each one holds a connection,
// so the sidebar keeps the selected tab's and the most recently shown others' up to a limit.

import { useEffect, useState } from "react";

/**
 * Shells one sidebar keeps attached, a split's members each counted. The shown tab's shells all
 * mount, even past it; the limit caps the hidden tabs kept beside them.
 */
export const MOUNTED_SHELL_TAB_LIMIT = 8;

/** `recent` with `key` moved to the front: the most recently shown shell first. */
export function noteShownShell(recent: readonly string[], key: string): readonly string[] {
  return recent[0] === key ? recent : [key, ...recent.filter((candidate) => candidate !== key)];
}

/**
 * The shells to keep mounted, counted one connection each: the selected tab's shells (its split's
 * members, all of them), then the most recently shown open tabs' shells while they fit in
 * `limit`, a split's members together. A closed sidebar keeps only the selected tab's shells.
 */
export function mountedShellKeys({
  recent,
  open,
  selected,
  shown,
  unitOf = (key) => [key],
  limit = MOUNTED_SHELL_TAB_LIMIT,
}: {
  recent: readonly string[];
  open: readonly string[];
  selected: string | null;
  shown: boolean;
  /** The shells that mount with `key`'s when its tab shows: its split's members, or it alone. */
  unitOf?: (key: string) => readonly string[];
  limit?: number;
}): string[] {
  const mounted = selected === null ? [] : [...new Set([selected, ...unitOf(selected)])];
  if (!shown) return mounted;
  const stillOpen = new Set(open);
  for (const key of recent) {
    if (!stillOpen.has(key) || mounted.includes(key)) continue;
    const unit = [...new Set([key, ...unitOf(key)])].filter((member) => !mounted.includes(member));
    if (mounted.length + unit.length <= limit) mounted.push(...unit);
  }
  return mounted;
}

/** The shells a sidebar keeps mounted as its selection moves among `open` shell tabs. */
export function useMountedShellKeys(
  open: readonly string[],
  selected: string | null,
  shown: boolean,
  unitOf?: (key: string) => readonly string[],
): string[] {
  const [recent, setRecent] = useState<readonly string[]>([]);
  useEffect(() => {
    if (selected !== null) setRecent((current) => noteShownShell(current, selected));
  }, [selected]);
  return mountedShellKeys({ recent, open, selected, shown, unitOf });
}
