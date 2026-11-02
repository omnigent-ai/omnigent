// Per-client slice key for host-less traffic.
//
// A sharded deployment routes a request that carries no slice key by its
// workspace id, so every key-less request from one workspace lands on the same
// replica. Host-less reads (session lists, `/health`, the
// `WS /v1/sessions/updates` stream) serve from any replica, so when no modal
// host is known they carry this random key instead. Each client generates its
// own key once and keeps it in localStorage, so a workspace's clients spread
// across replicas while one client stays on one replica across reloads and
// WebSocket reconnects. When storage is unavailable the key lasts for the page.

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
