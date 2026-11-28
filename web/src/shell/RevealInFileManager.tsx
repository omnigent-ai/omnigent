// "Show in Finder" for workspace files and folders. Offered only in the desktop
// shell, and only when the session's files are on this machine (its host is
// this machine's host), so a browser tab or a remote session sees no change.

import { createContext, useContext, useSyncExternalStore } from "react";
import { toast } from "sonner";
import { revealFile, supportsFileReveal } from "@/lib/nativeBridge";
import { useDesktopHostId } from "@/lib/desktopHostId";
import { FileViewerContext } from "./FileViewerContext";
import { getSessionHost, subscribeSessionHostChanges } from "@/lib/sessionHost";

/** Folder the Files tree is browsing; its row paths are relative to it. */
export const RevealBaseContext = createContext("");

export interface RevealTarget {
  hostId: string;
  /** Absolute path on the session's host. */
  path: string;
}

function joinPath(base: string, path: string): string {
  if (!path) return base;
  if (/^([A-Za-z]:)?[\\/]/.test(path)) return path;
  return `${base.replace(/[\\/]+$/, "")}/${path}`;
}

/** The viewed session's host: the viewer's own, else the one the session inherits. */
export function useSessionHostId(conversationId: string | null | undefined): string | null {
  const ctx = useContext(FileViewerContext);
  const inheritedHostId = useSyncExternalStore(subscribeSessionHostChanges, () =>
    conversationId ? getSessionHost(conversationId) : null,
  );
  return ctx?.sessionHostId || inheritedHostId;
}

/** Resolve a file against the session's host and currently browsed folder. */
export function useHostFileTarget(path: string | null): RevealTarget | null {
  const ctx = useContext(FileViewerContext);
  const base = useContext(RevealBaseContext);
  const hostId = useSessionHostId(ctx?.conversationId);
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
