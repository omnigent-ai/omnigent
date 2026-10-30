// Opening the server-built editor links on a session (Cursor / VS Code deep
// links). The server decides which links work for this viewer; the client only
// drops `local_only` links unless the session runs on this machine, remembers
// the default editor, and hands the link to the OS.

import { isElectronShell } from "./nativeBridge";
import type { EditorLink } from "./types";

export const DEFAULT_CODE_EDITOR_STORAGE_KEY = "omnigent:default-code-editor";

/** Links usable from this client: `local_only` ones need the session's host to be this machine. */
export function usableEditorLinks(
  links: EditorLink[] | undefined,
  sessionHostId: string | null | undefined,
  desktopHostId: string | null,
): EditorLink[] {
  if (!links) return [];
  const onThisMachine = !!sessionHostId && sessionHostId === desktopHostId;
  return links.filter((link) => !link.local_only || onThisMachine);
}

/** The remembered default if it is offered, else the server's first link. */
export function defaultEditorLink(
  links: EditorLink[],
  preferred: string | null,
): EditorLink | null {
  return links.find((link) => link.editor === preferred) ?? links[0] ?? null;
}

export function readDefaultCodeEditor(): string | null {
  try {
    return window.localStorage.getItem(DEFAULT_CODE_EDITOR_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeDefaultCodeEditor(editor: string): void {
  try {
    window.localStorage.setItem(DEFAULT_CODE_EDITOR_STORAGE_KEY, editor);
  } catch {
    // localStorage quota or access errors shouldn't break the app.
  }
}

/**
 * Hand an editor deep link to the OS. A browser tab loads it in a throwaway
 * iframe so the page doesn't navigate; the desktop shell routes `window.open`
 * through its protocol-consent prompt.
 */
export function launchEditorLink(url: string): void {
  if (isElectronShell()) {
    window.open(url, "_blank");
    return;
  }
  const iframe = document.createElement("iframe");
  iframe.style.display = "none";
  iframe.src = url;
  document.body.appendChild(iframe);
  window.setTimeout(() => iframe.remove(), 1000);
}
