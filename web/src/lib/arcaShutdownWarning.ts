import type { Host } from "@/hooks/useHosts";

const DISMISSED_KEY = "omnigent:arca-shutdown:dismissed";
const TOASTED_KEY = "omnigent:arca-shutdown:toasted";
const OPTED_OUT_KEY = "omnigent:arca-shutdown:opted-out";
export const ARCA_WARNING_PREFERENCES_CHANGED = "omnigent:arca-shutdown:preferences-changed";
const unavailableStorage = new Map<string, string>();

export function isArcaHost(
  host: Pick<Host, "host_id" | "name">,
  storedArcaHostId: string | null,
): boolean {
  return (
    host.name.endsWith("'s arca") ||
    (storedArcaHostId !== null && host.host_id === storedArcaHostId)
  );
}

export function isWarningWindow(now: Date): boolean {
  const day = now.getDay();
  return day >= 1 && day <= 5 && now.getHours() >= 17;
}

export function offersWorkweek(now: Date): boolean {
  const day = now.getDay();
  return day >= 1 && day <= 3;
}

export function dateKey(now: Date): string {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key) ?? unavailableStorage.get(key) ?? null;
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

export function isToastedToday(now: Date): boolean {
  return read(TOASTED_KEY) === dateKey(now);
}

export function markToastedToday(now: Date): void {
  write(TOASTED_KEY, dateKey(now));
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
