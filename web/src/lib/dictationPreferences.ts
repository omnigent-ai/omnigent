export type DictationPath = "auto" | "server" | "browser";

export interface DictationPreferences {
  path: DictationPath;
  browserLanguage: string;
  microphoneDeviceId: string | null;
}

export const DEFAULT_DICTATION_PREFERENCES: DictationPreferences = {
  path: "auto",
  browserLanguage: "en-US",
  microphoneDeviceId: null,
};

const STORAGE_KEY = "omnigent:dictation-preferences";
const MAX_LANGUAGE_LENGTH = 64;
const MAX_DEVICE_ID_LENGTH = 1024;

const isDictationPath = (value: unknown): value is DictationPath =>
  value === "auto" || value === "server" || value === "browser";

export function normalizeDictationLanguage(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > MAX_LANGUAGE_LENGTH) return null;
  try {
    return new Intl.Locale(trimmed).toString();
  } catch {
    return null;
  }
}

function normalizeDeviceId(value: unknown): string | null {
  if (typeof value !== "string" || !value || value.length > MAX_DEVICE_ID_LENGTH) return null;
  return value;
}

function normalizeDictationPreferences(value: DictationPreferences): DictationPreferences {
  return {
    path: isDictationPath(value.path) ? value.path : DEFAULT_DICTATION_PREFERENCES.path,
    browserLanguage:
      normalizeDictationLanguage(value.browserLanguage) ??
      DEFAULT_DICTATION_PREFERENCES.browserLanguage,
    microphoneDeviceId: normalizeDeviceId(value.microphoneDeviceId),
  };
}

export function readDictationPreferences(): DictationPreferences {
  if (typeof window === "undefined") return DEFAULT_DICTATION_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_DICTATION_PREFERENCES;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return DEFAULT_DICTATION_PREFERENCES;
    }
    const value = parsed as Record<string, unknown>;
    return {
      path: isDictationPath(value.path) ? value.path : DEFAULT_DICTATION_PREFERENCES.path,
      browserLanguage:
        normalizeDictationLanguage(value.browserLanguage) ??
        DEFAULT_DICTATION_PREFERENCES.browserLanguage,
      microphoneDeviceId: normalizeDeviceId(value.microphoneDeviceId),
    };
  } catch {
    return DEFAULT_DICTATION_PREFERENCES;
  }
}

export function writeDictationPreferences(preferences: DictationPreferences): DictationPreferences {
  const normalized = normalizeDictationPreferences(preferences);
  if (typeof window === "undefined") return normalized;
  try {
    if (
      normalized.path === DEFAULT_DICTATION_PREFERENCES.path &&
      normalized.browserLanguage === DEFAULT_DICTATION_PREFERENCES.browserLanguage &&
      normalized.microphoneDeviceId === DEFAULT_DICTATION_PREFERENCES.microphoneDeviceId
    ) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    }
  } catch {
    // localStorage access errors should not disable dictation.
  }
  return normalized;
}
