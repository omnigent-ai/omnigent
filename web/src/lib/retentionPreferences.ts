import { ARCHIVED_AT_LABEL_KEY } from "@/lib/sessionListCache";

const STORAGE_KEY = "omnigent:archived-retention-days";

export function readRetentionDays(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === null) return null;
    const parsed = parseInt(stored, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  } catch {
    return null;
  }
}

export function writeRetentionDays(days: number | null): void {
  if (typeof window === "undefined") return;
  try {
    if (days === null) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, String(days));
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}

/**
 * Epoch-seconds time a session was archived.
 *
 * Reads the server-written `omnigent.archived_at` label, falling back to
 * `updated_at` when it is absent (sessions archived before the label existed,
 * or a deployment that never wrote it). `updated_at` is bumped by archiving and
 * by any later edit, so it is never EARLIER than the true archive time: the
 * fallback can only under-report a session's age, which errs toward keeping a
 * session rather than expiring it.
 */
export function archivedAtSeconds(conversation: {
  labels?: Record<string, string>;
  updated_at: number;
}): number {
  const raw = conversation.labels?.[ARCHIVED_AT_LABEL_KEY];
  const parsed = raw === undefined ? NaN : parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : conversation.updated_at;
}
