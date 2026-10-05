// Import a design system: copy its slide-relevant files from the user's folder
// into the design's workspace, then point the design at the copy. The listing,
// reads, and writes are injected so the rules here stay unit-testable.

import type { HostDirectoryListing } from "@/hooks/useHostFilesystem";
import { FONT_MIME, IMAGE_MIME, extension, type KitFile } from "@/shell/codeViewerHelpers";
import {
  DESIGN_SYSTEM_IMPORT_DIR,
  DESIGN_SYSTEM_POINTER,
  DS_MANIFEST,
  DS_SKILL,
  serializeDesignSystemPointer,
  type DesignSystemRef,
} from "./designSystem";
import { DS_ASSET_MAX_BYTES, DS_DECK_MAX_BYTES, DS_STYLESHEET } from "./designSystemInjection";

export const IMPORT_FILES = [DS_SKILL, "README.md", DS_MANIFEST, DS_STYLESHEET];
export const IMPORT_FOLDERS = ["fonts", "assets", "templates", "slides"];
export const NEVER_IMPORTED = ["uploads", "ui_kits", "preview"];
export const IMPORT_CONCURRENCY = 4;
// The viewer's image and font types, plus the text the agent and viewer read.
const IMPORT_EXTENSIONS = new Set([
  ...Object.keys(IMAGE_MIME),
  ...Object.keys(FONT_MIME),
  "css",
  "html",
  "md",
  "json",
]);
const MB = 1024 * 1024;

/** A file relative to the design-system folder. */
export interface SourceFile {
  path: string;
  bytes: number;
}

export interface SourceListing {
  files: SourceFile[];
  /** Top-level folder names, so never-imported ones can be reported. */
  folders: string[];
}

export interface ImportPlan {
  files: SourceFile[];
  skipped: { path: string; reason: string }[];
  totalBytes: number;
}

export interface ImportError {
  path: string;
  message: string;
}

export type ListHostDir = (absolute: string) => Promise<HostDirectoryListing>;

async function listAll(dir: string, list: ListHostDir) {
  const listing = await list(dir);
  if (listing.truncated) throw new Error(`${dir} has too many files to import`);
  return listing.entries;
}

async function walk(dir: string, rel: string, list: ListHostDir): Promise<SourceFile[]> {
  const entries = await listAll(dir, list);
  const files = entries
    .filter((e) => e.type === "file")
    .map((e) => ({ path: `${rel}/${e.name}`, bytes: e.bytes ?? 0 }));
  const nested = await Promise.all(
    entries
      .filter((e) => e.type === "directory")
      .map((e) => walk(e.path, `${rel}/${e.name}`, list)),
  );
  return [...files, ...nested.flat()];
}

/** List the allowlisted files under `folder`; other folders are never walked. */
export async function listDesignSystemSource(
  folder: string,
  list: ListHostDir,
): Promise<SourceListing> {
  const top = await listAll(folder, list);
  const files = top
    .filter((e) => e.type === "file" && IMPORT_FILES.includes(e.name))
    .map((e) => ({ path: e.name, bytes: e.bytes ?? 0 }));
  const dirs = top.filter((e) => e.type === "directory");
  const nested = await Promise.all(
    dirs.filter((e) => IMPORT_FOLDERS.includes(e.name)).map((e) => walk(e.path, e.name, list)),
  );
  return { files: [...files, ...nested.flat()], folders: dirs.map((e) => e.name) };
}

function skipReason(file: SourceFile): string | null {
  const segments = file.path.split("/");
  if (segments.some((s) => !s || s === "." || s === ".." || s.includes("\\"))) {
    return "unsupported name";
  }
  if (segments.length === 1)
    return IMPORT_FILES.includes(file.path) ? null : "not part of a design system";
  if (!IMPORT_FOLDERS.includes(segments[0])) return "not part of a design system";
  return IMPORT_EXTENSIONS.has(extension(file.path)) ? null : "not an allowed file type";
}

/** What an import copies and skips, in path order, within the per-file and total caps. */
export function planDesignSystemImport(source: SourceListing): ImportPlan {
  const plan: ImportPlan = { files: [], skipped: [], totalBytes: 0 };
  const sorted = [...source.files].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
  for (const file of sorted) {
    let reason = skipReason(file);
    if (!reason && file.bytes > DS_ASSET_MAX_BYTES) {
      reason = `larger than ${DS_ASSET_MAX_BYTES / MB} MB`;
    } else if (!reason && plan.totalBytes + file.bytes > DS_DECK_MAX_BYTES) {
      reason = `over the ${DS_DECK_MAX_BYTES / MB} MB total`;
    }
    if (reason) {
      plan.skipped.push({ path: file.path, reason });
    } else {
      plan.files.push(file);
      plan.totalBytes += file.bytes;
    }
  }
  for (const name of [...source.folders].sort()) {
    if (NEVER_IMPORTED.includes(name))
      plan.skipped.push({ path: `${name}/`, reason: "never imported" });
  }
  return plan;
}

export interface ImportDeps {
  /** Read a file relative to the source folder; `null` when it is missing. */
  read: (path: string) => Promise<KitFile | null>;
  /** Write a workspace file. */
  write: (path: string, content: string, encoding: "utf-8" | "base64") => Promise<void>;
  onProgress: (done: number, total: number) => void;
}

async function copyOne(file: SourceFile, deps: ImportDeps): Promise<void> {
  const found = await deps.read(file.path);
  if (!found) throw new Error("not found");
  if (found.truncated) throw new Error("too long to import (the read was truncated)");
  await deps.write(`${DESIGN_SYSTEM_IMPORT_DIR}/${file.path}`, found.content, found.encoding);
}

/**
 * Copy the plan a few files at a time, then write the pointer, only when every
 * file copied, so a half-finished import is never used.
 */
export async function importDesignSystem(
  plan: ImportPlan,
  source: DesignSystemRef,
  deps: ImportDeps,
): Promise<{ errors: ImportError[]; ref: DesignSystemRef | null }> {
  const failed = new Map<number, ImportError>();
  let next = 0;
  let done = 0;
  const worker = async () => {
    while (next < plan.files.length) {
      const index = next++;
      const file = plan.files[index];
      try {
        // oxlint-disable-next-line no-await-in-loop
        await copyOne(file, deps);
      } catch (e) {
        failed.set(index, { path: file.path, message: e instanceof Error ? e.message : String(e) });
      }
      done += 1;
      deps.onProgress(done, plan.files.length);
    }
  };
  await Promise.all(Array.from({ length: IMPORT_CONCURRENCY }, worker));
  const errors = [...failed.entries()].sort(([a], [b]) => a - b).map(([, e]) => e);
  if (errors.length) return { errors, ref: null };
  const ref: DesignSystemRef = {
    path: DESIGN_SYSTEM_IMPORT_DIR,
    kind: source.kind,
    name: source.name,
    importedFrom: source.path,
  };
  await deps.write(DESIGN_SYSTEM_POINTER, serializeDesignSystemPointer(ref), "utf-8");
  return { errors: [], ref };
}
