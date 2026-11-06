import { useSyncExternalStore } from "react";
import { getBasePath } from "./basePath";
import {
  getOmnigentHostConfig,
  getOmnigentServerIdentity,
  hostFetch,
  isDatabricksWorkspace,
} from "./host";
import { randomUUID } from "./randomUUID";

const CHANGE_KEY_PREFIX = "omnigent:admin-mode-change:";
const listeners = new Set<() => void>();
let adminMode: boolean | null = null;
let userId: string | null = null;
let browserModeRequested = false;
let modeCookieName: string | null = null;
let initialCookie: string | undefined;
let resolvedScope: string | null = null;

function cookiePath(): string | null {
  const path = isDatabricksWorkspace()
    ? (getOmnigentHostConfig().adminModeCookiePath ?? null)
    : getBasePath() || "/";
  return path?.startsWith("/") && !/[;\r\n]/.test(path) ? path : null;
}

function readCookie(name: string): string | undefined {
  try {
    return document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(`${name}=`));
  } catch {
    return undefined;
  }
}

function changeKey(): string {
  return `${CHANGE_KEY_PREFIX}${JSON.stringify([getOmnigentServerIdentity(), cookiePath(), modeCookieName, userId])}`;
}

function bootstrapScope(): string {
  return JSON.stringify([getOmnigentServerIdentity(), cookiePath(), browserModeRequested]);
}

/** Opt into the identity bootstrap before any app requests start. */
export function initializeAdminMode(): void {
  browserModeRequested = cookiePath() !== null && typeof document !== "undefined";
}

export function usesBrowserAdminMode(): boolean {
  return browserModeRequested;
}

/** Cached identity cannot authorize a remount after its deployment or cookie changes. */
export function hasAdminModeContextChanged(): boolean {
  return (
    browserModeRequested &&
    ((resolvedScope !== null && resolvedScope !== bootstrapScope()) ||
      (modeCookieName !== null &&
        initialCookie !== undefined &&
        readCookie(modeCookieName) !== initialCookie))
  );
}

/** Watch mode changes only while the app is mounted, preserving identity on remount. */
export function subscribeAdminModeChanges(): () => void {
  const onStorage = (event: StorageEvent) => {
    if (adminMode !== null && event.key === changeKey() && event.newValue) location.reload();
  };
  const onPageShow = (event: PageTransitionEvent) => {
    if (event.persisted && adminMode !== null) location.reload();
  };
  const onFocus = () => {
    if (hasAdminModeContextChanged()) location.reload();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener("pageshow", onPageShow);
  window.addEventListener("focus", onFocus);
  onFocus();
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener("pageshow", onPageShow);
    window.removeEventListener("focus", onFocus);
  };
}

/** Supporting servers must establish the scoped cookie before app requests can start. */
export function resolveAdminMode(data: {
  user_id: string | null;
  can_enable_admin_mode?: boolean;
  admin_mode_enabled?: boolean;
  admin_mode_cookie_name?: string;
}): boolean {
  resolvedScope = bootstrapScope();
  userId = data.user_id;
  modeCookieName =
    typeof data.admin_mode_cookie_name === "string" &&
    /^omnigent_admin_mode_[a-zA-Z0-9_]+$/.test(data.admin_mode_cookie_name)
      ? data.admin_mode_cookie_name
      : null;
  initialCookie = modeCookieName ? readCookie(modeCookieName) : undefined;
  const supportsAdminMode =
    data.admin_mode_cookie_name !== undefined ||
    data.can_enable_admin_mode !== undefined ||
    data.admin_mode_enabled !== undefined;
  const ready = !browserModeRequested || !supportsAdminMode || initialCookie !== undefined;
  adminMode =
    browserModeRequested &&
    ready &&
    modeCookieName !== null &&
    data.can_enable_admin_mode === true &&
    typeof data.admin_mode_enabled === "boolean"
      ? data.admin_mode_enabled
      : null;
  for (const listener of listeners) listener();
  return ready;
}

export function getAdminMode(): boolean | null {
  return browserModeRequested ? adminMode : null;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Null means the server, transport, or account does not offer admin mode. */
export function useAdminMode(): boolean | null {
  return useSyncExternalStore(subscribe, getAdminMode, () => null);
}

/** Reload every view after the cookie takes effect, dropping cached permissions and streams. */
export async function setAdminMode(enabled: boolean): Promise<void> {
  if (getAdminMode() === null) throw new Error("Admin mode is unavailable for this account.");
  const changeFailed = "Could not change admin mode. Try again.";
  try {
    const response = await hostFetch("/v1/me/admin-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok) throw new Error(changeFailed);
  } catch {
    throw new Error(changeFailed);
  }
  try {
    localStorage.setItem(changeKey(), randomUUID());
  } catch {
    // Other tabs also check their cookie when they regain focus.
  }
  location.reload();
}
