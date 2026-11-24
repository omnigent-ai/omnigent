// Daily "What's New" entries bundled from entries/YYYY-MM-DD.md, plus the
// per-browser marker of the newest entry date the user has already seen.

export interface WhatsNewEntry {
  /** `YYYY-MM-DD`, from the file name. */
  date: string;
  title: string;
  /** Markdown after the title heading. */
  body: string;
}

export const WHATS_NEW_LAST_SEEN_KEY = "omnigent:whats-new:last-seen";
/** With no marker yet, only entries this recent count as new. */
export const FIRST_VISIT_WINDOW_DAYS = 14;

const ENTRY_FILE = /(\d{4}-\d{2}-\d{2})\.md$/;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/** Parse `{ path: markdown }` into entries, newest first; skips misnamed files. */
export function parseEntries(files: Record<string, string>): WhatsNewEntry[] {
  const entries: WhatsNewEntry[] = [];
  for (const [path, raw] of Object.entries(files)) {
    const match = ENTRY_FILE.exec(path);
    if (!match) continue;
    const text = raw.replace(/^﻿/, "").trim();
    const heading = /^#\s+(.+)$/m.exec(text);
    const title = heading ? heading[1].trim() : "What's New";
    const body = heading ? text.slice(heading.index + heading[0].length).trim() : text;
    entries.push({ date: match[1], title, body });
  }
  return entries.sort((a, b) => b.date.localeCompare(a.date));
}

function isoDateDaysAgo(now: Date, days: number): string {
  const past = new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
  return past.toISOString().slice(0, 10);
}

/** Entries the user hasn't seen, newest first. */
export function unseenEntries(
  entries: WhatsNewEntry[],
  lastSeen: string | null,
  now: Date = new Date(),
): WhatsNewEntry[] {
  const after = lastSeen ?? isoDateDaysAgo(now, FIRST_VISIT_WINDOW_DAYS);
  return entries.filter((entry) => entry.date > after);
}

export function readLastSeen(): string | null {
  try {
    const value = window.localStorage.getItem(WHATS_NEW_LAST_SEEN_KEY);
    return value && DATE_ONLY.test(value) ? value : null;
  } catch {
    return null;
  }
}

export function writeLastSeen(date: string): void {
  try {
    window.localStorage.setItem(WHATS_NEW_LAST_SEEN_KEY, date);
  } catch {
    // Storage unavailable (private mode, quota): the badge just reappears next load.
  }
}

const bundledFiles = import.meta.glob<string>("./entries/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
});

export const WHATS_NEW_ENTRIES: WhatsNewEntry[] = parseEntries(bundledFiles);
