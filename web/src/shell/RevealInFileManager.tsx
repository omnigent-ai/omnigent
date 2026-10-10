// "Show in Finder" for workspace files and folders. Offered only in the desktop
// shell, and only when the session's files are on this machine (its host is
// this machine's host), so a browser tab or a remote session sees no change.

import { createContext, useContext, useSyncExternalStore } from "react";
import { toast } from "sonner";
import {
  getHostIdentity,
  onHostStatusChanged,
  revealFile,
  supportsFileReveal,
} from "@/lib/nativeBridge";
import { FileViewerContext } from "./FileViewerContext";

/** Folder the Files tree is browsing; its row paths are relative to it. */
export const RevealBaseContext = createContext("");

export interface RevealTarget {
  hostId: string;
  /** Absolute path on this machine. */
  path: string;
}

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

function subscribeToHostId(listener: () => void) {
  if (!watchingHost && supportsFileReveal()) {
    watchingHost = true;
    readLocalHostId();
    onHostStatusChanged(readLocalHostId);
  }
  hostIdListeners.add(listener);
  return () => {
    hostIdListeners.delete(listener);
  };
}

function useLocalHostId(): string | null {
  const hostId = useSyncExternalStore(subscribeToHostId, () => localHostId);
  return supportsFileReveal() ? hostId : null;
}

function joinPath(base: string, path: string): string {
  if (!path) return base;
  if (/^([A-Za-z]:)?[\\/]/.test(path)) return path;
  return `${base.replace(/[\\/]+$/, "")}/${path}`;
}

/** Where ``path`` lives on this machine, or null when it can't be revealed here. */
export function useRevealTarget(path: string | null): RevealTarget | null {
  const ctx = useContext(FileViewerContext);
  const base = useContext(RevealBaseContext);
  const hostId = useLocalHostId();
  const root = ctx?.workspaceRoot;
  if (path === null || !root || !hostId || ctx?.sessionHostId !== hostId) return null;
  return { hostId, path: joinPath(joinPath(root, base), path) };
}

/** "Show in Finder" for a file (selected in its folder), "Open in Finder" for a folder. */
export function revealLabel(directory: boolean): string {
  const userAgent = navigator.userAgent;
  const app = userAgent.includes("Macintosh")
    ? "Finder"
    : userAgent.includes("Windows")
      ? "File Explorer"
      : "file manager";
  return `${directory ? "Open" : "Show"} in ${app}`;
}

export function revealInFileManager(target: RevealTarget): void {
  void revealFile(target.hostId, target.path).then((ok) => {
    if (!ok) toast.error("Couldn't show this item in the file manager");
  });
}
