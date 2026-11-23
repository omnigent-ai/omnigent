// Per-device preference for what the sidebar Inbox badge counts. "all" (the
// default) counts everything the Inbox lists, including sessions with a reply
// you haven't read. "attention" leaves those unread sessions out, so the number
// only moves for approvals, comments and failed automations. The Inbox page and
// the sidebar's unread dots are the same either way.

import { useSyncExternalStore } from "react";

export type InboxBadgeCount = "all" | "attention";

export const INBOX_BADGE_COUNT_STORAGE_KEY = "omnigent:inbox-badge-count";

export const DEFAULT_INBOX_BADGE_COUNT: InboxBadgeCount = "all";

const CHANGE_EVENT = "omnigent:inbox-badge-count-change";

/** Read the preference; malformed or unreadable storage yields the default. */
export function readInboxBadgeCount(): InboxBadgeCount {
  if (typeof window === "undefined") return DEFAULT_INBOX_BADGE_COUNT;
  try {
    return window.localStorage.getItem(INBOX_BADGE_COUNT_STORAGE_KEY) === "attention"
      ? "attention"
      : DEFAULT_INBOX_BADGE_COUNT;
  } catch {
    return DEFAULT_INBOX_BADGE_COUNT;
  }
}

/** Persist the preference, clearing the key for the default; false if storage refused it. */
export function writeInboxBadgeCount(value: InboxBadgeCount): boolean {
  if (typeof window === "undefined") return false;
  try {
    if (value === "attention") {
      window.localStorage.setItem(INBOX_BADGE_COUNT_STORAGE_KEY, "attention");
    } else {
      window.localStorage.removeItem(INBOX_BADGE_COUNT_STORAGE_KEY);
    }
  } catch {
    // localStorage failures should not make Settings unusable.
    return false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return true;
}

/** Call `listener` when this tab saves the preference or another tab changes it. */
export function subscribeInboxBadgeCount(listener: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === INBOX_BADGE_COUNT_STORAGE_KEY) listener();
  };
  window.addEventListener(CHANGE_EVENT, listener);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** The current preference, kept live so the badge follows a change made in Settings. */
export function useInboxBadgeCount(): InboxBadgeCount {
  return useSyncExternalStore(
    subscribeInboxBadgeCount,
    readInboxBadgeCount,
    () => DEFAULT_INBOX_BADGE_COUNT,
  );
}
