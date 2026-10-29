import { isElectronShell } from "./nativeBridge";

export const CODE_EDITORS = [
  { id: "vscode", label: "VS Code" },
  { id: "cursor", label: "Cursor" },
] as const;
export type CodeEditor = (typeof CODE_EDITORS)[number]["id"];

export function normalizeSshTarget(value: unknown): string | null {
  return typeof value === "string" &&
    value.length <= 255 &&
    value === value.trim() &&
    /^(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*$/.test(value)
    ? value
    : null;
}

/** Build a protocol link for an absolute path on a known local or SSH host. */
export function editorLink(
  editor: CodeEditor,
  path: string,
  sshTarget: string | null,
  directory = false,
): string | null {
  // oxlint-disable-next-line no-control-regex -- Control characters cannot be editor path arguments.
  if (!CODE_EDITORS.some(({ id }) => id === editor) || /[\x00-\x1f\x7f]/.test(path)) return null;
  let absolutePath = path;
  if (!sshTarget && /^[A-Za-z]:[\\/]/.test(path)) absolutePath = `/${path.replaceAll("\\", "/")}`;
  if (!absolutePath.startsWith("/")) return null;
  if (sshTarget !== null && !normalizeSshTarget(sshTarget)) return null;
  const location =
    sshTarget === null ? "file" : `vscode-remote/ssh-remote+${encodeURIComponent(sshTarget)}`;
  // Remote protocol handlers distinguish files from folders by a line suffix.
  const selection = sshTarget && !directory ? ":1:1" : "";
  return `${editor}://${location}${absolutePath.split("/").map(encodeURIComponent).join("/")}${selection}`;
}

/** Hand off during the click gesture; the shell/browser owns protocol consent. */
export function launchEditorLink(url: string): void {
  if (isElectronShell()) {
    window.open(url, "_blank", "noopener,noreferrer");
    return;
  }
  const frame = document.createElement("iframe");
  frame.hidden = true;
  frame.src = url;
  document.body.appendChild(frame);
  window.setTimeout(() => frame.remove(), 1000);
}
