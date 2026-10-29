import { useCallback, useEffect, useRef, useState } from "react";
import { checkHostDirectory, useHostFilesystem } from "@/hooks/useHostFilesystem";
import { useRecentWorkspaces } from "@/hooks/useRecentWorkspaces";
import { normalizeWorkspacePath } from "./NewChatDialog";
import { parentOf, resolveWorkspacePath } from "./WorkspacePicker";

/**
 * The working-directory field of the switch dialogs for a destination host.
 * It defaults to the session's directory when that host has it, else the
 * host's last-used directory, else its home. Typed text is never overwritten.
 * Pass a null host when no launch is needed.
 */
export function useSwitchWorkspace({
  open,
  hostId,
  currentWorkspace,
}: {
  open: boolean;
  hostId: string | null;
  currentWorkspace: string | null;
}) {
  const [workspace, setWorkspace] = useState("");
  const { recent, addRecent } = useRecentWorkspaces(hostId);
  // Shares the picker's query key, so browsing afterwards costs no request.
  const { data: homeListing, isPlaceholderData } = useHostFilesystem(
    hostId,
    hostId === null ? null : "",
  );
  // Entries share one parent, so the first entry resolves the host's home.
  const firstHomeEntry = homeListing && !isPlaceholderData ? homeListing.entries[0] : undefined;
  const resolvedHome = firstHomeEntry ? parentOf(firstHomeEntry.path) : null;

  // A path means nothing on another machine: clear it when the host changes.
  const previousHostId = useRef<string | null>(null);
  const userEditedRef = useRef(false);
  useEffect(() => {
    if (previousHostId.current === hostId) return;
    previousHostId.current = hostId;
    setWorkspace("");
    userEditedRef.current = false;
  }, [hostId]);

  // Until the probe settles the default waits, so a slow answer can't lose to
  // the recent-directory fallback. Any failure just means "no".
  const [sessionDirProbe, setSessionDirProbe] = useState<{
    hostId: string;
    path: string;
    exists: boolean;
  } | null>(null);
  useEffect(() => {
    if (!open || hostId === null || !currentWorkspace) return undefined;
    let cancelled = false;
    void checkHostDirectory(hostId, currentWorkspace)
      .then(
        (problem) => problem === null,
        () => false,
      )
      .then((exists) => {
        if (!cancelled) setSessionDirProbe({ hostId, path: currentWorkspace, exists });
      });
    return () => {
      cancelled = true;
    };
  }, [open, hostId, currentWorkspace]);
  const probeMatches =
    sessionDirProbe !== null &&
    sessionDirProbe.hostId === hostId &&
    sessionDirProbe.path === currentWorkspace;
  const sessionDirSettled = !currentWorkspace || probeMatches;
  const sessionDirExists = !!currentWorkspace && probeMatches && sessionDirProbe.exists;

  useEffect(() => {
    if (!open || hostId === null || workspace !== "" || userEditedRef.current) return;
    if (!sessionDirSettled) return;
    const preferred = (sessionDirExists ? currentWorkspace : null) ?? recent[0] ?? resolvedHome;
    if (preferred) setWorkspace(preferred);
  }, [
    open,
    hostId,
    workspace,
    recent,
    resolvedHome,
    sessionDirSettled,
    sessionDirExists,
    currentWorkspace,
  ]);

  const onChange = useCallback((next: string) => {
    userEditedRef.current = true;
    setWorkspace(next);
  }, []);

  const reset = useCallback(() => {
    setWorkspace("");
    previousHostId.current = null;
    userEditedRef.current = false;
  }, []);

  // The server never expands "~", so resolve it against the host's home.
  const resolved = resolveWorkspacePath(workspace, resolvedHome);
  return {
    workspace,
    /** Absolute path to submit; "" until a typed "~/…" can resolve. */
    path: resolved ?? normalizeWorkspacePath(workspace) ?? "",
    valid: resolved !== null,
    recent,
    addRecent,
    onChange,
    reset,
  };
}
