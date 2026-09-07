// Persisted default surface for terminal-first chat transcripts.
//
// Terminal-first sessions expose both the rendered Chat transcript and the
// agent's live terminal. This preference chooses which surface opens when a
// session has no per-tab view selection; AppShell's sessionStorage state still
// wins after the user switches a particular chat.

const STORAGE_KEY = "omnigent:default-transcript-view";

export const transcriptViewDefaults = ["chat", "terminal"] as const;
export type TranscriptViewDefault = (typeof transcriptViewDefaults)[number];

/** Preserve the existing product behavior for users without a saved choice. */
export const TRANSCRIPT_VIEW_DEFAULT: TranscriptViewDefault = "chat";

/** Return whether a string is one of the supported transcript surfaces. */
export function isTranscriptViewDefault(
  value: string | null | undefined,
): value is TranscriptViewDefault {
  return value === "chat" || value === "terminal";
}

/** Normalize storage drift or manual edits to the product default. */
export function normalizeTranscriptViewDefault(
  value: string | null | undefined,
): TranscriptViewDefault {
  return isTranscriptViewDefault(value) ? value : TRANSCRIPT_VIEW_DEFAULT;
}

/**
 * Read the default surface for terminal-first transcripts.
 *
 * Returns "chat" when no preference exists, during server rendering, or when
 * localStorage is unavailable/corrupt.
 */
export function readTranscriptViewDefault(): TranscriptViewDefault {
  if (typeof window === "undefined") return TRANSCRIPT_VIEW_DEFAULT;
  try {
    return normalizeTranscriptViewDefault(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    return TRANSCRIPT_VIEW_DEFAULT;
  }
}

/** Persist a transcript default, clearing storage for the product default. */
export function writeTranscriptViewDefault(value: TranscriptViewDefault): void {
  if (typeof window === "undefined") return;
  try {
    const normalized = normalizeTranscriptViewDefault(value);
    if (normalized === TRANSCRIPT_VIEW_DEFAULT) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, normalized);
    }
  } catch {
    // localStorage quota or access errors shouldn't break settings.
  }
}
