// This machine's host id from the desktop shell, which decides whether a session runs here:
// its files are local, and its terminals end when the app stops a host it started.

import { useSyncExternalStore } from "react";

import { getHostIdentity, isElectronShell, onHostStatusChanged } from "@/lib/nativeBridge";

// This machine's host id, re-read whenever the host's status changes: a fresh
// install has none until its first host connection.
let localHostId: string | null = null;
let watchingHost = false;
const hostIdListeners = new Set<() => void>();

function readLocalHostId() {
  void getHostIdentity().then((identity) => {
    const next = identity?.hostId ?? null;
    if (next === localHostId) return;
    localHostId = next;
    hostIdListeners.forEach((listener) => listener());
  });
}

function watchHostId(shouldWatch: boolean, listener: () => void) {
  if (!watchingHost && shouldWatch) {
    watchingHost = true;
    readLocalHostId();
    onHostStatusChanged(readLocalHostId);
  }
  hostIdListeners.add(listener);
  return () => {
    hostIdListeners.delete(listener);
  };
}

const subscribeToDesktopHostId = (listener: () => void) => watchHostId(isElectronShell(), listener);

/** This machine's host id from the desktop shell; null in a browser or before its first connect. */
export function useDesktopHostId(): string | null {
  return useSyncExternalStore(subscribeToDesktopHostId, () => localHostId);
}

/** Forgets the host id read so far, so the next reader asks the shell again; for tests. */
export function resetDesktopHostIdForTests(): void {
  localHostId = null;
  watchingHost = false;
}
