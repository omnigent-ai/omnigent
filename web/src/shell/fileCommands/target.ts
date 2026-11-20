// What a file action acts on: one file or folder, named both from the workspace
// root and absolutely on the session's host. Surfaces name files differently
// (the Files tree relative to the folder it browses, chat links as the agent
// wrote them), so each resolves its path here before asking for an action.

import { useContext, useMemo, useSyncExternalStore } from "react";
import { absoluteWorkspacePath, isAbsoluteComposerPath } from "@/lib/composerContext";
import { getSessionHost, subscribeSessionHostChanges } from "@/lib/sessionHost";
import { FileViewerContext, useFileViewerConversationId } from "../FileViewerContext";
import { RevealBaseContext } from "../RevealInFileManager";

export interface FileCommandTarget {
  /** Its path from the workspace root; `null` when it's outside the workspace. */
  relativePath: string | null;
  /** Its absolute path on the session's host; `null` until the workspace root is known. */
  absolutePath: string | null;
  /** The session it belongs to, which links point into; `null` outside a session. */
  conversationId: string | null;
  /** The host the session runs on; `null` until known. */
  hostId: string | null;
  directory: boolean;
  deleted: boolean;
  /** A file in a repository, such as a pull request's: its repository path is its path. */
  repositoryFile?: boolean;
}

function trimTrailingSeparators(path: string): string {
  return path.replace(/[\\/]+$/, "");
}

/** `path` joined onto `base` with the base's separator, unless it's absolute. */
function join(base: string, path: string): string {
  if (!path) return base;
  return absoluteWorkspacePath(path, base) ?? path;
}

/** `absolute` from `root`, or `null` when it's not inside it. */
function relativeTo(root: string, absolute: string): string | null {
  const prefix = trimTrailingSeparators(root);
  if (absolute === prefix) return "";
  for (const separator of ["/", "\\"]) {
    if (absolute.startsWith(prefix + separator)) return absolute.slice(prefix.length + 1);
  }
  return null;
}

/**
 * Resolve `path`, which is relative to `base` (itself relative to the workspace
 * root, or absolute) unless it's absolute.
 */
export function resolveFileCommandTarget(
  path: string,
  {
    root,
    base = "",
    conversationId = null,
    hostId,
    directory = false,
    deleted = false,
    repositoryFile = false,
  }: {
    root: string | null;
    base?: string;
    conversationId?: string | null;
    hostId: string | null;
    directory?: boolean;
    deleted?: boolean;
    repositoryFile?: boolean;
  },
): FileCommandTarget {
  const joined = join(base, path);
  const kind = repositoryFile ? { repositoryFile } : {};
  if (isAbsoluteComposerPath(joined)) {
    return {
      relativePath: root ? relativeTo(root, joined) : null,
      absolutePath: joined,
      conversationId,
      hostId,
      directory,
      deleted,
      ...kind,
    };
  }
  return {
    relativePath: joined,
    absolutePath: root ? join(root, joined) : null,
    conversationId,
    hostId,
    directory,
    deleted,
    ...kind,
  };
}

/** The session's host, as the file viewer context or the session list knows it. */
export function useSessionHostId(): string | null {
  const context = useContext(FileViewerContext);
  const inherited = useSyncExternalStore(subscribeSessionHostChanges, () =>
    context?.conversationId ? getSessionHost(context.conversationId) : null,
  );
  return context?.sessionHostId || inherited || null;
}

export interface FileCommandTargetOptions {
  directory?: boolean;
  deleted?: boolean;
  /** The session a surface shows, when it knows it better than the file viewer context. */
  conversationId?: string;
}

/** `path` as the surface names it, resolved against the workspace and the browsed folder. */
export function useFileCommandTarget(
  path: string | null,
  {
    directory = false,
    deleted = false,
    conversationId: ownConversationId,
  }: FileCommandTargetOptions = {},
): FileCommandTarget | null {
  const root = useContext(FileViewerContext)?.workspaceRoot ?? null;
  const base = useContext(RevealBaseContext);
  const contextConversationId = useFileViewerConversationId();
  const conversationId = ownConversationId ?? contextConversationId ?? null;
  const hostId = useSessionHostId();
  return useMemo(
    () =>
      path === null
        ? null
        : resolveFileCommandTarget(path, {
            root,
            base,
            conversationId,
            hostId,
            directory,
            deleted,
          }),
    [path, root, base, conversationId, hostId, directory, deleted],
  );
}

/** The file actions that copy a path. */
export type CopyPathCommandId = "copy-path" | "copy-relative-path";

/**
 * What a copy action puts on the clipboard. "Copy path" copies the full path
 * once the workspace location is known, else the path from the workspace root;
 * "Copy relative path" copies the path from the root, or the full path for a
 * file outside the workspace or the root itself.
 */
export function pathToCopy(
  target: FileCommandTarget,
  command: CopyPathCommandId = "copy-path",
): { text: string; absolute: boolean } | null {
  const relative = target.relativePath;
  if (command === "copy-relative-path" && relative !== null && relative !== "") {
    return { text: relative, absolute: false };
  }
  if (target.absolutePath !== null) return { text: target.absolutePath, absolute: true };
  if (relative !== null) return { text: relative, absolute: false };
  return null;
}

/** Identifies a target, so a request built for one file never acts on another. */
export function fileCommandTargetKey(target: FileCommandTarget): string {
  return JSON.stringify([
    target.relativePath,
    target.absolutePath,
    target.conversationId,
    target.hostId,
    target.directory,
    target.deleted,
  ]);
}
