import { isElectronShell } from "./nativeBridge";

export const CODE_EDITORS = [
  { id: "vscode", label: "VS Code" },
  { id: "cursor", label: "Cursor" },
] as const;
export type CodeEditor = (typeof CODE_EDITORS)[number]["id"];

export function normalizeSshTarget(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 255 || value !== value.trim()) return null;
  const parts = value.split("@");
  return parts.length <= 2 && parts.every((part) => /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(part))
    ? value
    : null;
}

interface EditorHost {
  name: string;
  sandbox_provider?: string | null;
}

const ARCLET_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const ARCLET_RESERVED_PREFIX_PATTERN = /^arclet[_-]/i;

/** Infer only the standard SSH aliases provisioned for Databricks hosts. */
export function inferHostSshTarget(host: EditorHost | null | undefined): string | null {
  if (host?.sandbox_provider === "databricks-arca") return "arca.ssh";
  if (typeof host?.name !== "string" || host.name !== host.name.trim()) return null;
  if (
    host.sandbox_provider !== "arclet" ||
    !ARCLET_NAME_PATTERN.test(host.name) ||
    ARCLET_RESERVED_PREFIX_PATTERN.test(host.name)
  )
    return null;
  return normalizeSshTarget(`arclet_${host.name}.ssh`);
}

/** Read the unique canonical machine type; the image marker is shared by other hosts. */
export function parseArcaTypeLabel(content: string): "arca" | "arclet" | null {
  const labels = content
    .split(/\r?\n/)
    .map((line) => line.trim().split(/\s+/))
    .filter((fields) => fields[0] === "arca_type");
  if (labels.length !== 1 || labels[0].length !== 2) return null;
  const value = labels[0][1];
  return value === "arca" || value === "arclet" ? value : null;
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
  // A frame-src CSP can block custom schemes in iframes, even with a wildcard.
  const link = document.createElement("a");
  link.href = url;
  link.target = "_self";
  link.rel = "noreferrer";
  document.body.append(link);
  link.click();
  link.remove();
}
