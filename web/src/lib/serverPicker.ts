import { getOmnigentHostConfig } from "./host";
import {
  getServerPicker as getNativeServerPicker,
  isNativeShell,
  openServerSetup as openNativeServerSetup,
  switchServer as switchNativeServer,
  type ServerPickerInfo,
} from "./nativeBridge";

const STORAGE_KEY = "omnigent:web-server-recents";
const TRANSFER_KEY = "omnigent-server-recents";
const MAX_RECENT_SERVERS = 5;
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

export type ServerPickerRuntime = "desktop" | "web";

export interface UnifiedServerPickerInfo extends ServerPickerInfo {
  runtime: ServerPickerRuntime;
}

/** Normalize user-entered server URLs with the same defaults as Electron. */
export function normalizeServerUrl(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) throw new Error("Enter a server URL.");
  const host = (() => {
    try {
      return new URL(`https://${trimmed}`).hostname;
    } catch {
      return "";
    }
  })();
  const value = trimmed.includes("://")
    ? trimmed
    : `${LOCAL_HOSTS.has(host) ? "http" : "https"}://${trimmed}`;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Enter a valid server URL.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Server URLs must use HTTP or HTTPS.");
  }
  url.hash = "";
  return url.toString();
}

function compactServers(values: unknown[]): string[] {
  const servers: string[] = [];
  for (const value of values) {
    if (typeof value !== "string") continue;
    try {
      const normalized = normalizeServerUrl(value);
      if (!servers.includes(normalized)) servers.push(normalized);
    } catch {
      // Ignore malformed stored/transferred entries.
    }
    if (servers.length === MAX_RECENT_SERVERS) break;
  }
  return servers;
}

function readStoredServers(): string[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? compactServers(JSON.parse(raw) as unknown[]) : [];
  } catch {
    return [];
  }
}

function writeStoredServers(servers: string[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(servers));
  } catch {
    // Storage failures only disable persistence.
  }
}

function consumeTransferredServers(): string[] {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const raw = params.get(TRANSFER_KEY);
  if (!raw) return [];
  params.delete(TRANSFER_KEY);
  const remaining = params.toString();
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}${remaining ? `#${remaining}` : ""}`,
  );
  try {
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? compactServers(parsed) : [];
  } catch {
    return [];
  }
}

function currentWebServer(): string {
  return normalizeServerUrl(window.location.origin);
}

function rememberServers(...servers: string[]): string[] {
  const recents = compactServers([...servers, ...readStoredServers()]);
  writeStoredServers(recents);
  return recents;
}

function navigateWebServer(raw: string): void {
  const target = normalizeServerUrl(raw);
  const recents = rememberServers(target, currentWebServer());
  const destination = new URL(target);
  const params = new URLSearchParams(destination.hash.replace(/^#/, ""));
  params.set(TRANSFER_KEY, JSON.stringify(recents));
  destination.hash = params.toString();
  window.location.href = destination.toString();
}

/** Shared picker data for Electron and standalone web. */
export async function getServerPicker(): Promise<UnifiedServerPickerInfo | null> {
  const native = await getNativeServerPicker();
  if (native) return { ...native, runtime: "desktop" };
  if (typeof window === "undefined" || isNativeShell() || getOmnigentHostConfig().fetcher) {
    return null;
  }

  const currentOrigin = currentWebServer();
  const recentServers = rememberServers(currentOrigin, ...consumeTransferredServers());
  return { currentOrigin, recentServers, runtime: "web" };
}

export async function switchServer(url: string, runtime: ServerPickerRuntime): Promise<void> {
  if (runtime === "desktop") {
    await switchNativeServer(url);
    return;
  }
  navigateWebServer(url);
}

export function openServerSetup(): void {
  openNativeServerSetup();
}

export function connectWebServer(url: string): void {
  navigateWebServer(url);
}
