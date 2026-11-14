// "Show in Finder" for workspace files and folders. Offered only in the desktop
// shell, and only when the session's files are on this machine (its host is
// this machine's host), so a browser tab or a remote session sees no change.

import { createContext, useContext, useSyncExternalStore } from "react";
import { toast } from "sonner";
import {
  getHostIdentity,
  isElectronShell,
  onHostStatusChanged,
  revealFile,
  supportsFileReveal,
} from "@/lib/nativeBridge";
import { FileViewerContext } from "./FileViewerContext";
import { getSessionHost, subscribeSessionHostChanges } from "@/lib/sessionHost";

/** Folder the Files tree is browsing; its row paths are relative to it. */
export const RevealBaseContext = createContext("");

export interface RevealTarget {
  hostId: string;
  /** Absolute path on the session's host. */
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

function joinPath(base: string, path: string): string {
  if (!path) return base;
  if (/^([A-Za-z]:)?[\\/]/.test(path)) return path;
  return `${base.replace(/[\\/]+$/, "")}/${path}`;
}

/** Resolve a file against the session's host and currently browsed folder. */
export function useHostFileTarget(path: string | null): RevealTarget | null {
  const ctx = useContext(FileViewerContext);
  const base = useContext(RevealBaseContext);
  const inheritedHostId = useSyncExternalStore(subscribeSessionHostChanges, () =>
    ctx?.conversationId ? getSessionHost(ctx.conversationId) : null,
  );
  const hostId = ctx?.sessionHostId || inheritedHostId;
  const root = ctx?.workspaceRoot;
  if (path === null || !root || !hostId) return null;
  return { hostId, path: joinPath(joinPath(root, base), path) };
}

/** This OS's file manager name: "Finder", "File Explorer", or "file manager". */
export function fileManagerName(): string {
  const userAgent = navigator.userAgent;
  if (userAgent.includes("Macintosh")) return "Finder";
  if (userAgent.includes("Windows")) return "File Explorer";
  return "file manager";
}

/** Where ``path`` lives, only when this desktop owns the session's host. */
export function useLocalFileTarget(path: string | null): RevealTarget | null {
  const target = useHostFileTarget(path);
  const hostId = useDesktopHostId();
  return target?.hostId === hostId ? target : null;
}

/** Where ``path`` lives on this machine, or null when it can't be revealed here. */
export function useRevealTarget(path: string | null): RevealTarget | null {
  const target = useLocalFileTarget(path);
  return supportsFileReveal() ? target : null;
}

/** "Show in Finder" for a file (selected in its folder), "Open in Finder" for a folder. */
export function revealLabel(directory: boolean): string {
  return `${directory ? "Open" : "Show"} in ${fileManagerName()}`;
}

export function revealInFileManager(target: RevealTarget): void {
  void revealFile(target.hostId, target.path).then((ok) => {
    if (!ok) toast.error("Couldn't show this item in the file manager");
  });
}
