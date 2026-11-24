import { useMemo, type ReactNode } from "react";
import { useWorkspaceEnvironment } from "@/hooks/useWorkspaceChangedFiles";
import { useNavigate } from "@/lib/routing";
import { getSessionSharePath } from "@/lib/shareLink";
import { FileViewerContext, type OpenFileOptions } from "@/shell/FileViewerContext";
import { citedFilePosition } from "@/shell/filePositionState";

const NEVER_CHANGED = () => false;

/** The session route that opens `path` in its file viewer, at the cited position if any. */
export function ticketFileHref(sessionId: string, path: string, options?: OpenFileOptions): string {
  const params = new URLSearchParams({ file: path });
  const position = citedFilePosition(options);
  if (position) {
    params.set("line", String(position.line));
    if (position.column) params.set("column", String(position.column));
    if (position.endLine) params.set("endLine", String(position.endLine));
  }
  return `${getSessionSharePath(sessionId)}?${params}`;
}

/**
 * Resolves file paths in ticket markdown against `sessionId`'s workspace, so the
 * chat's path links work here too and open the file in that session's viewer.
 * Without a session, paths stay plain text.
 */
export function TicketFileLinks({
  sessionId,
  children,
}: {
  sessionId: string | null | undefined;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  const environment = useWorkspaceEnvironment(sessionId ?? undefined);
  const value = useMemo(
    () =>
      sessionId
        ? {
            openFile: (path: string, options?: OpenFileOptions) =>
              void navigate(ticketFileHref(sessionId, path, options)),
            openGithubTab: () => void navigate(getSessionSharePath(sessionId)),
            isChangedPath: NEVER_CHANGED,
            conversationId: sessionId,
            workspaceRoot: environment.data?.root ?? null,
            workspaceHome: environment.data?.home ?? null,
          }
        : null,
    [sessionId, navigate, environment.data],
  );
  return <FileViewerContext.Provider value={value}>{children}</FileViewerContext.Provider>;
}
