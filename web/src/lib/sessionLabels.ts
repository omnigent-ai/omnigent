// User-facing session labels ("tags"): which `conversation_labels` keys a
// person may see and edit, and the limits the server's label columns impose.

import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";

/** Width of the `conversation_labels.key` column. */
export const LABEL_KEY_MAX_LENGTH = 128;
/** Width of the `conversation_labels.value` column. */
export const LABEL_VALUE_MAX_LENGTH = 256;

/** Namespaces Omnigent writes for its own bookkeeping (pins, wrappers, cost). */
const INTERNAL_LABEL_PREFIXES = ["omnigent.", "cost_control."] as const;

/** Whether a label key is framework-owned rather than a user label; mirrors the server's `is_internal_label_key`. */
export function isInternalLabelKey(key: string): boolean {
  return (
    key === PROJECT_LABEL_KEY || INTERNAL_LABEL_PREFIXES.some((prefix) => key.startsWith(prefix))
  );
}

/** The user labels in `labels`, internal keys dropped, sorted by key. */
export function userLabelEntries(
  labels: Record<string, string> | null | undefined,
): [key: string, value: string][] {
  return Object.entries(labels ?? {})
    .filter(([key]) => !isInternalLabelKey(key))
    .sort(([a], [b]) => a.localeCompare(b));
}

/**
 * Validate a label about to be saved, returning a message for the user or
 * `null` when it can be saved. The key is expected trimmed.
 */
export function labelValidationError(key: string, value: string): string | null {
  if (!key) return "Key is required.";
  if (key.length > LABEL_KEY_MAX_LENGTH) {
    return `Key must be ${LABEL_KEY_MAX_LENGTH} characters or fewer.`;
  }
  if (isInternalLabelKey(key)) return `"${key}" is reserved for Omnigent.`;
  if (value.length > LABEL_VALUE_MAX_LENGTH) {
    return `Value must be ${LABEL_VALUE_MAX_LENGTH} characters or fewer.`;
  }
  return null;
}

/**
 * Read a label map from untyped JSON (e.g. a project's stored config), keeping
 * only string values so a malformed entry can't reach the editor.
 */
export function labelRecordFromUnknown(raw: unknown): Record<string, string> {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return {};
  return Object.fromEntries(
    Object.entries(raw).filter((entry): entry is [string, string] => typeof entry[1] === "string"),
  );
}

/**
 * The labels a project's stored config stamps on new sessions, dropping any
 * entry the server would skip (internal, too long, or not a string).
 */
export function projectDefaultLabels(config: { labels?: unknown } | null | undefined) {
  return Object.fromEntries(
    Object.entries(labelRecordFromUnknown(config?.labels)).filter(
      ([key, value]) => labelValidationError(key, value) === null,
    ),
  );
}

/**
 * A new session's labels: the project's defaults with this session's edits on
 * top, where a `null` edit leaves that default off this session. `labels` is
 * what the session will start with; `requestLabels` keeps the `null`s so the
 * create request tells the server which defaults to leave off.
 */
export function newSessionLabels(
  projectDefaults: Record<string, string>,
  edits: Record<string, string | null>,
): { labels: Record<string, string>; requestLabels: Record<string, string | null> } {
  const requestLabels = { ...projectDefaults, ...edits };
  const labels = Object.fromEntries(
    Object.entries(requestLabels).filter((entry): entry is [string, string] => entry[1] !== null),
  );
  return { labels, requestLabels };
}

/** Whether two label maps hold the same entries. */
export function labelsEqual(a: Record<string, string>, b: Record<string, string>): boolean {
  const aKeys = Object.keys(a);
  return aKeys.length === Object.keys(b).length && aKeys.every((key) => b[key] === a[key]);
}
