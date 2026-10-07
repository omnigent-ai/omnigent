import { ARCA_HOST_ID_STORAGE_KEY } from "./arcaHost";

const DISMISSED_KEY = "omnigent:arca-shutdown:dismissed";
const WARNED_KEY = "omnigent:arca-shutdown:warned";
const OPTED_OUT_KEY = "omnigent:arca-shutdown:opted-out";
export const ARCA_WARNING_PREFERENCES_CHANGED = "omnigent:arca-shutdown:preferences-changed";
// Tab-local fallback keeps preferences usable when localStorage throws, as in private mode.
const unavailableStorage = new Map<string, string>();
const WARNING_START_HOUR = 17;
const FIRST_WEEKDAY = 1;
const LAST_WEEKDAY = 5;
const LAST_WORKWEEK_OFFER_DAY = 3;

export function isArcaWarningStorageKey(key: string | null): boolean {
  return (
    key === null ||
    key === DISMISSED_KEY ||
    key === WARNED_KEY ||
    key === OPTED_OUT_KEY ||
    key === ARCA_HOST_ID_STORAGE_KEY
  );
}

export function resetArcaWarningStorageForTests(): void {
  unavailableStorage.clear();
}

export function isWarningWindow(now: Date): boolean {
  const day = now.getDay();
  return day >= FIRST_WEEKDAY && day <= LAST_WEEKDAY && now.getHours() >= WARNING_START_HOUR;
}

export function offersWorkweek(now: Date): boolean {
  const day = now.getDay();
  // Thursday and Friday have no useful workweek extension before Friday shutdown.
  return day >= FIRST_WEEKDAY && day <= LAST_WORKWEEK_OFFER_DAY;
}

export function dateKey(now: Date): string {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function read(key: string): string | null {
  try {
    return unavailableStorage.get(key) ?? localStorage.getItem(key) ?? null;
  } catch {
    return unavailableStorage.get(key) ?? null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
    unavailableStorage.delete(key);
  } catch {
    unavailableStorage.set(key, value);
  }
}

export function isDismissedToday(now: Date): boolean {
  return read(DISMISSED_KEY) === dateKey(now);
}

export function dismissToday(now: Date): void {
  write(DISMISSED_KEY, dateKey(now));
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(ARCA_WARNING_PREFERENCES_CHANGED));
  }
}

export function isWarnedToday(now: Date): boolean {
  return read(WARNED_KEY) === dateKey(now);
}

export function markWarnedToday(now: Date): void {
  write(WARNED_KEY, dateKey(now));
}

export function isOptedOut(): boolean {
  return read(OPTED_OUT_KEY) === "true";
}

export function optOut(): void {
  write(OPTED_OUT_KEY, "true");
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(ARCA_WARNING_PREFERENCES_CHANGED));
  }
}
