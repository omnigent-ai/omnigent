// A target's file command facts, derived the same way for ⌘K, every file menu,
// and the host that carries the actions out. What depends on the session's host
// is read once as an environment, so a surface listing many files (a diff
// review) derives each file's facts without a hook per file.

import { useMemo } from "react";
import { useHostSshTarget } from "@/hooks/useHostSshTarget";
import { CODE_EDITORS, editorLink, type CodeEditor } from "@/lib/editorLinks";
import { isAndroidShell, isIOSShell, supportsFileReveal } from "@/lib/nativeBridge";
import { useDesktopHostId } from "@/lib/desktopHostId";
import { fileManagerName } from "../RevealInFileManager";
import type { FileCommandFacts } from "./model";
import type { FileCommandTarget } from "./target";

/** What the facts read from the app and the session's host, shared by its files. */
export interface FileCommandEnvironment {
  /** The session's host is this machine. */
  local: boolean;
  /** The SSH alias that reaches the session's remote host, if any. */
  sshTarget: string | null;
  /** The files belong to a session, so a link can point into it. */
  linkable: boolean;
  /** The desktop app can show files in the file manager. */
  revealSupported: boolean;
  fileManager: string;
}

/** The editor link for `target`, or `null` when `editor` can't open it from here. */
export function fileEditorLink(
  editor: CodeEditor,
  target: FileCommandTarget,
  environment: Pick<FileCommandEnvironment, "local" | "sshTarget">,
): string | null {
  if (!target.absolutePath || (!environment.local && !environment.sshTarget)) return null;
  return editorLink(
    editor,
    target.absolutePath,
    environment.local ? null : environment.sshTarget,
    target.directory,
  );
}

/** How the asking surface can open files, which only it knows. */
export interface SurfaceOpenFacts {
  openable?: boolean;
  newTabOpenable?: boolean;
  rootable?: boolean;
}

/** The facts for `target` in `environment`; the open facts are the surface's own answer. */
export function fileCommandFacts(
  target: FileCommandTarget,
  environment: FileCommandEnvironment,
  { openable = false, newTabOpenable = false, rootable = false }: SurfaceOpenFacts = {},
): FileCommandFacts {
  return {
    directory: target.directory,
    deleted: target.deleted,
    relativePath: target.relativePath,
    absolutePath: target.absolutePath,
    openable,
    newTabOpenable,
    rootable,
    linkable: environment.linkable,
    revealable: environment.local && environment.revealSupported && target.absolutePath !== null,
    fileManager: environment.fileManager,
    editors: CODE_EDITORS.flatMap(({ id }) =>
      fileEditorLink(id, target, environment) ? [id] : [],
    ),
  };
}

/**
 * The environment for session `conversationId`'s files on `hostId`. Editors open this
 * machine's files directly and a remote host's over SSH; a phone opens neither.
 */
export function useFileCommandEnvironment(
  hostId: string | null,
  conversationId: string | null,
): FileCommandEnvironment {
  const desktopHostId = useDesktopHostId();
  const mobile = isAndroidShell() || isIOSShell();
  const local = !mobile && hostId !== null && hostId === desktopHostId;
  const sshTarget = useHostSshTarget(
    !mobile && !local ? hostId : null,
    conversationId ?? undefined,
  );
  return useMemo(
    () => ({
      local,
      sshTarget,
      linkable: conversationId !== null,
      revealSupported: supportsFileReveal(),
      fileManager: fileManagerName(),
    }),
    [local, sshTarget, conversationId],
  );
}

/** The facts for `target`; the open facts are the surface's own answer, as only it knows what it shows. */
export function useFileCommandFacts(
  target: FileCommandTarget | null,
  { openable = false, newTabOpenable = false, rootable = false }: SurfaceOpenFacts = {},
): FileCommandFacts | null {
  const environment = useFileCommandEnvironment(
    target?.hostId ?? null,
    target?.conversationId ?? null,
  );
  return useMemo(
    () =>
      target ? fileCommandFacts(target, environment, { openable, newTabOpenable, rootable }) : null,
    [target, environment, openable, newTabOpenable, rootable],
  );
}
