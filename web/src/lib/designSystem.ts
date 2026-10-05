// Design systems for the Design page: kind detection from marker files, the
// display name, the `.omnigent/design-system.json` pointer, recent systems
// per host, and the agent instruction. Pure, so it is unit-testable.

export const DESIGN_SYSTEM_POINTER = ".omnigent/design-system.json";
/** Where an imported copy lives in the design's workspace. */
export const DESIGN_SYSTEM_IMPORT_DIR = ".omnigent/design-system";
export const DS_MANIFEST = "_ds_manifest.json";
export const DS_SKILL = "SKILL.md";
export const NOT_A_DESIGN_SYSTEM = "Not a design system: no SKILL.md or _ds_manifest.json";
export const DESIGN_SYSTEM_RECENTS_MAX = 5;
const NAME_MAX = 80;
const RECENTS_KEY = "omnigent.design.systems";

export type DesignSystemKind = "full" | "skill";

/** What the pointer stores. `path` is absolute, or relative to the workspace. */
export interface DesignSystemRef {
  path: string;
  kind: DesignSystemKind;
  name: string;
  /** The absolute folder an imported copy came from. */
  importedFrom?: string;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** Kind from a folder's entry names; `null` when it is not a design system. */
export function detectDesignSystemKind(names: readonly string[]): DesignSystemKind | null {
  if (names.includes(DS_MANIFEST)) return "full";
  if (names.includes(DS_SKILL)) return "skill";
  return null;
}

export function folderName(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).at(-1) ?? path;
}

function manifestNamespace(text: string | undefined): string {
  if (!text) return "";
  try {
    const raw: unknown = JSON.parse(text);
    return isObject(raw) && typeof raw.namespace === "string" ? raw.namespace.trim() : "";
  } catch {
    return "";
  }
}

function frontmatterName(text: string | undefined): string {
  const front = text?.match(/^---\r?\n([\s\S]*?)\r?\n---/)?.[1] ?? "";
  const value = front.match(/^name:\s*(.+)$/m)?.[1].trim() ?? "";
  return value.replace(/^(["'])(.*)\1$/, "$2").trim();
}

/** The manifest `namespace`, else the `SKILL.md` frontmatter `name`, else the folder name. */
export function designSystemName(source: {
  folder: string;
  manifest?: string;
  skill?: string;
}): string {
  const name =
    manifestNamespace(source.manifest) ||
    frontmatterName(source.skill) ||
    folderName(source.folder);
  return name.slice(0, NAME_MAX);
}

/** Parse and validate the pointer. Throws an Error whose message is user-facing. */
export function parseDesignSystemPointer(text: string): DesignSystemRef {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new Error("design-system.json is not valid JSON");
  }
  if (!isObject(raw)) throw new Error("design-system.json must be a JSON object");
  const path = typeof raw.path === "string" ? raw.path.trim().replace(/(.)[/\\]+$/, "$1") : "";
  if (!path || path.includes("\0")) throw new Error('design-system.json needs a "path"');
  if (path.split(/[/\\]/).some((s) => s === "." || s === "..")) {
    throw new Error('"path" must not contain "." or ".." segments');
  }
  if (raw.kind !== "full" && raw.kind !== "skill") {
    throw new Error('"kind" must be "full" or "skill"');
  }
  const name = (typeof raw.name === "string" ? raw.name.trim() : "") || folderName(path);
  const ref: DesignSystemRef = { path, kind: raw.kind, name: name.slice(0, NAME_MAX) };
  if (typeof raw.imported_from === "string" && raw.imported_from) {
    ref.importedFrom = raw.imported_from;
  }
  return ref;
}

export function serializeDesignSystemPointer(ref: DesignSystemRef): string {
  const { path, kind, name, importedFrom } = ref;
  const pointer = { path, kind, name, ...(importedFrom ? { imported_from: importedFrom } : {}) };
  return `${JSON.stringify(pointer, null, 2)}\n`;
}

/** An absolute host path (POSIX, drive letter, or UNC) rather than a workspace path. */
export function isAbsoluteDesignSystemPath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

export function designSystemInstruction(ref: DesignSystemRef): string {
  return `Follow the design system at \`${ref.path}\` (\`${ref.kind}\`). Read its SKILL.md first.`;
}

function readAllRecents(): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENTS_KEY) ?? "{}");
    return isObject(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Recent design systems for a host, most recent first. */
export function readRecentDesignSystems(hostId: string): DesignSystemRef[] {
  const list = readAllRecents()[hostId];
  if (!Array.isArray(list)) return [];
  return list.flatMap((entry) => {
    try {
      return [parseDesignSystemPointer(JSON.stringify(entry))];
    } catch {
      return [];
    }
  });
}

export function rememberDesignSystem(hostId: string, ref: DesignSystemRef): void {
  const rest = readRecentDesignSystems(hostId).filter((r) => r.path !== ref.path);
  const next = {
    ...readAllRecents(),
    [hostId]: [ref, ...rest].slice(0, DESIGN_SYSTEM_RECENTS_MAX),
  };
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Storage disabled or full: recents just won't be offered next time.
  }
}
