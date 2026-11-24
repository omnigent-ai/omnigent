// Hands a conversation opened in an ordinary browser to the Omnigent desktop
// app when it is running. The app listens on a loopback port and accepts only
// pages of servers it is connected to (electron/src/desktopOpenServer.js).

import { toast } from "sonner";

import { isNativeShell } from "./nativeBridge";
import { isSessionWindow } from "./sessionWindow";

/** Loopback ports the desktop app may listen on, tried in order. Mirrors the app's own list. */
export const DESKTOP_OPEN_PORTS = [28417, 28418, 28419];

const PREFERENCE_KEY = "omnigent:open-in-desktop-app";
/** Set once this browser has been asked, so a user who ignores the question isn't asked again. */
const OFFERED_KEY = "omnigent:open-in-desktop-app-offered";
/** Set once this tab handed its conversation over, so a reload stays in the browser. */
const HANDED_OVER_KEY = "omnigent:open-in-desktop-app-done";

/** Long enough for an app on this machine to answer, short enough to go unnoticed. */
const AUTOMATIC_TIMEOUT_MS = 1500;
/** The browser may be asking the user for permission while this request waits. */
const REQUESTED_TIMEOUT_MS = 60_000;
/** The app answers once a window shows the conversation, which can mean loading one first. */
const OPEN_TIMEOUT_MS = 12_000;

/** Set on every answer from the app's listener; another program on the port doesn't send it. */
const LISTENER_HEADER = "X-Omnigent-Desktop";

/** Names browsers have used for the permission that guards requests to this machine. */
const LOOPBACK_PERMISSIONS = ["loopback-network", "local-network-access"];

/** Hostnames of a server on this machine. */
const LOCAL_HOSTNAMES = ["localhost", "127.0.0.1", "[::1]"];

/** The stored choice: undefined until the user has made one. */
function storedPreference(): boolean | undefined {
  try {
    const stored = window.localStorage.getItem(PREFERENCE_KEY);
    return stored === null ? undefined : stored === "true";
  } catch {
    return undefined;
  }
}

/** Per-browser preference; off until the user accepts the offer or turns it on. */
export function readOpenInDesktopApp(): boolean {
  return storedPreference() === true;
}

export function writeOpenInDesktopApp(value: boolean): void {
  try {
    window.localStorage.setItem(PREFERENCE_KEY, String(value));
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/**
 * Whether this browser lets the page reach the desktop app without asking.
 *
 * `granted` covers a browser that has no such permission at all. `unsupported`
 * is a browser where the state can't be read, so nothing is attempted.
 */
export async function desktopAppPermission(): Promise<
  "granted" | "prompt" | "denied" | "unsupported"
> {
  const permissions = navigator.permissions;
  if (!permissions?.query) return "unsupported";
  // A page served from this machine reaches it without the permission, whatever its state says.
  if (LOCAL_HOSTNAMES.includes(window.location.hostname)) return "granted";
  for (const name of LOOPBACK_PERMISSIONS) {
    try {
      // oxlint-disable-next-line no-await-in-loop -- the names are alternatives, tried in order
      return (await permissions.query({ name: name as PermissionName })).state;
    } catch {
      // This browser doesn't know the name; try the next.
    }
  }
  return "granted";
}

/**
 * The port the desktop app answers on for this page's server, or null when no
 * app is running, it isn't connected to this server, or the browser blocked
 * the request. Opens nothing.
 *
 * @param timeoutMs - How long each port gets to answer.
 */
async function desktopAppPort(timeoutMs: number): Promise<number | null> {
  for (const port of DESKTOP_OPEN_PORTS) {
    try {
      // The request goes to the app on this machine, not to the server, so it bypasses the host transport.
      // oxlint-disable-next-line no-await-in-loop, no-restricted-globals -- ports are tried one after another
      const response = await fetch(`http://127.0.0.1:${port}/status`, {
        credentials: "omit",
        signal: AbortSignal.timeout(timeoutMs),
      });
      // Another program holds this port; the app may be on the next.
      if (!response.headers.has(LISTENER_HEADER)) continue;
      return response.status === 204 ? port : null;
    } catch {
      // Nothing is listening here, or the app doesn't know this server.
    }
  }
  return null;
}

/**
 * Whether the desktop app is running and connected to this page's server.
 * Opens nothing; used to offer the hand-off only to someone who can use it.
 */
export async function desktopAppRunning(): Promise<boolean> {
  return (await desktopAppPort(AUTOMATIC_TIMEOUT_MS)) !== null;
}

/**
 * Ask the running desktop app to open a conversation on this page's server.
 *
 * @param conversationId - The session to open, e.g. `"e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"`.
 * @param findTimeoutMs - How long each port gets to say the app is there.
 * @returns Whether the app is showing it. False when no app is running, the
 *   app isn't connected to this server, the browser blocked the request, or
 *   the app accepted it but could not bring the conversation up.
 */
export async function openInDesktopApp(
  conversationId: string,
  findTimeoutMs: number = AUTOMATIC_TIMEOUT_MS,
): Promise<boolean> {
  const port = await desktopAppPort(findTimeoutMs);
  if (port === null) return false;
  try {
    // oxlint-disable-next-line no-restricted-globals -- to the app on this machine, not the server
    const response = await fetch(`http://127.0.0.1:${port}/open-conversation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversationId }),
      credentials: "omit",
      signal: AbortSignal.timeout(OPEN_TIMEOUT_MS),
    });
    // The app answers 204 only once one of its windows shows the conversation.
    return response.headers.has(LISTENER_HEADER) && response.status === 204;
  } catch {
    return false;
  }
}

function isMobileBrowser(): boolean {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
}

/** Whether this page can hand over at all: an ordinary browser on a desktop. */
export function desktopAppHandoffSupported(): boolean {
  return !isNativeShell() && !isMobileBrowser();
}

/** True while a hand-off is being decided, so a repeated call can't ask twice. */
let handoffInFlight = false;

function flagged(storage: () => Storage, key: string): boolean {
  try {
    return storage().getItem(key) !== null;
  } catch {
    return false;
  }
}

function flag(storage: () => Storage, key: string): void {
  try {
    storage().setItem(key, "1");
  } catch {
    // Without storage the hand-off simply isn't remembered.
  }
}

/** The conversation this tab handed to the app, while the page says so; null otherwise. */
let handedOverConversation: string | null = null;
const handoffListeners = new Set<() => void>();

function setHandedOverConversation(conversationId: string | null): void {
  handedOverConversation = conversationId;
  for (const listener of handoffListeners) listener();
}

/** Subscribe to {@link handedOverConversationId}; returns the unsubscribe. */
export function subscribeToDesktopAppHandoff(listener: () => void): () => void {
  handoffListeners.add(listener);
  return () => handoffListeners.delete(listener);
}

/** The conversation the app just took from this tab, until the user stays in the browser. */
export function handedOverConversationId(): string | null {
  return handedOverConversation;
}

/** The user chose to carry on in the browser: show the conversation again. */
export function continueInBrowser(): void {
  setHandedOverConversation(null);
}

/**
 * The `omnigent://` link for a conversation on this page's server.
 *
 * @param conversationId - e.g. `"e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"`.
 * @returns e.g. `"omnigent://omnigent.example.com/c/e4f5…"`; null for a remote
 *   server over plain http, which a link can't express.
 */
export function desktopAppDeepLink(conversationId: string): string | null {
  const { protocol, hostname, host } = window.location;
  const expected = LOCAL_HOSTNAMES.includes(hostname) ? "http:" : "https:";
  return protocol === expected ? `omnigent://${host}/c/${conversationId}` : null;
}

function announceOpened(conversationId: string): void {
  flag(() => window.sessionStorage, HANDED_OVER_KEY);
  setHandedOverConversation(conversationId);
}

/**
 * Ask whether conversations should open in the desktop app. Accepting turns
 * the preference on and opens this one; the browser's own permission prompt,
 * where it has one, follows the click.
 */
function offerDesktopApp(conversationId: string, browserWillAsk: boolean): void {
  toast("Open conversations in the Omnigent desktop app?", {
    description: browserWillAsk
      ? "Your browser will ask to let this site reach apps on this device."
      : "Links you open in this browser will move to the app when it's running.",
    duration: 20_000,
    testId: "desktop-app-handoff-offer",
    action: {
      label: "Open in app",
      onClick: () => {
        writeOpenInDesktopApp(true);
        void openInDesktopApp(conversationId, REQUESTED_TIMEOUT_MS).then((opened) => {
          if (opened) announceOpened(conversationId);
          else toast("Couldn't open this conversation in the Omnigent desktop app.");
        });
      },
    },
  });
}

/**
 * Handle only the tab's landing conversation, without triggering a background
 * permission prompt.
 *
 * @param conversationId - The session in the address the tab was opened with.
 */
export async function handOffToDesktopApp(conversationId: string): Promise<void> {
  if (handoffInFlight) return;
  handoffInFlight = true;
  try {
    await decideHandOff(conversationId);
  } finally {
    handoffInFlight = false;
  }
}

async function decideHandOff(conversationId: string): Promise<void> {
  const preference = storedPreference();
  if (!desktopAppHandoffSupported() || isSessionWindow() || preference === false) return;
  if (flagged(() => window.sessionStorage, HANDED_OVER_KEY)) return;
  const permission = await desktopAppPermission();
  if (permission !== "granted" && permission !== "prompt") return;
  if (preference === true && permission === "granted") {
    if (await openInDesktopApp(conversationId)) announceOpened(conversationId);
    return;
  }
  if (preference === undefined) {
    if (flagged(() => window.localStorage, OFFERED_KEY)) return;
    // Where the browser lets the page look, don't ask someone who isn't running the app.
    if (permission === "granted" && !(await desktopAppRunning())) return;
    flag(() => window.localStorage, OFFERED_KEY);
  }
  offerDesktopApp(conversationId, permission === "prompt");
}
