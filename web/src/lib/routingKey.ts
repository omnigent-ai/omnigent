// Random per-client slice key for host-less requests when no modal host is known. Unkeyed
// requests route by workspace id onto one replica; this spreads clients across replicas.
// Stored in localStorage so a browser's tabs, reloads, and reconnects share one replica.

import { randomUUID } from "@/lib/randomUUID";

const STORAGE_KEY = "omnigent:routing-key";
const KEY_RE = /^[0-9a-f]{32}$/;

let clientKey: string | null = null;

function readStoredKey(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored !== null && KEY_RE.test(stored) ? stored : null;
  } catch {
    return null;
  }
}

function writeStoredKey(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, key);
  } catch {
    // Without storage the key still holds for this page.
  }
}

/** This client's random slice key, created and stored on first use. */
export function clientRoutingKey(): string {
  if (clientKey !== null) return clientKey;
  clientKey = readStoredKey();
  if (clientKey === null) {
    clientKey = randomUUID().replace(/-/g, "");
    writeStoredKey(clientKey);
  }
  return clientKey;
}
