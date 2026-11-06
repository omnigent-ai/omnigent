import { useSyncExternalStore } from "react";
import { getBasePath } from "./basePath";
import {
  getOmnigentHostConfig,
  getOmnigentServerIdentity,
  hostFetch,
  isDatabricksWorkspace,
} from "./host";

const COOKIE_NAME = "omnigent_admin_mode";
const CHANGE_KEY_PREFIX = "omnigent:admin-mode-change:";
const listeners = new Set<() => void>();
let adminMode: boolean | null = null;
let userId: string | null = null;
let initialized = false;
let cookieReady = false;
let modeCookieName: string | null = null;
let initialCookie: string | undefined;

function cookiePath(): string | null {
  const path = isDatabricksWorkspace()
    ? (getOmnigentHostConfig().adminModeCookiePath ?? null)
    : getBasePath() || "/";
  return path?.startsWith("/") && !/[;\r\n]/.test(path) ? path : null;
}

function readCookie(name: string = COOKIE_NAME): string | undefined {
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

/** Initialize before app requests so a fresh browser starts with ordinary access. */
export function initializeAdminMode(): void {
  const path = cookiePath();
  if (path === null || typeof document === "undefined") return;
  if (readCookie() !== `${COOKIE_NAME}=off`) {
    try {
      document.cookie = `${COOKIE_NAME}=off; Path=${path}; SameSite=Lax${location.protocol === "https:" ? "; Secure" : ""}`;
    } catch {
      return;
    }
  }
  cookieReady = readCookie() !== undefined;
  if (initialized || !cookieReady) return;
  initialized = true;
  window.addEventListener("storage", (event) => {
    if (adminMode !== null && event.key === changeKey() && event.newValue) location.reload();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && adminMode !== null) location.reload();
  });
  window.addEventListener("focus", () => {
    if (adminMode !== null && modeCookieName && readCookie(modeCookieName) !== initialCookie) {
      location.reload();
    }
  });
}

/** Only a supporting backend and eligible account can offer the switch. */
export function resolveAdminMode(data: {
  user_id: string | null;
  can_enable_admin_mode?: boolean;
  admin_mode_enabled?: boolean;
  admin_mode_cookie_name?: string;
}): void {
  userId = data.user_id;
  modeCookieName =
    typeof data.admin_mode_cookie_name === "string" &&
    /^omnigent_admin_mode_[a-zA-Z0-9_]+$/.test(data.admin_mode_cookie_name)
      ? data.admin_mode_cookie_name
      : null;
  adminMode =
    cookieReady &&
    cookiePath() !== null &&
    modeCookieName !== null &&
    data.can_enable_admin_mode === true &&
    typeof data.admin_mode_enabled === "boolean"
      ? data.admin_mode_enabled
      : null;
  initialCookie = modeCookieName ? readCookie(modeCookieName) : undefined;
  for (const listener of listeners) listener();
}

export function getAdminMode(): boolean | null {
  return adminMode;
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
  if (adminMode === null) throw new Error("Admin mode is unavailable for this account.");
  const response = await hostFetch("/v1/me/admin-mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!response.ok) throw new Error("Could not change admin mode. Try again.");
  try {
    localStorage.setItem(changeKey(), crypto.randomUUID());
  } catch {
    // Other tabs also check their cookie when they regain focus.
  }
  location.reload();
}
