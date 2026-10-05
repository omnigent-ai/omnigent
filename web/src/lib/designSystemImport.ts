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
export const MAX_WALK_DEPTH = 8;
export const MAX_LISTED_ENTRIES = 2000;
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

/** A file relative to the design-system folder; `bytes` is `null` when unknown. */
export interface SourceFile {
  path: string;
  bytes: number | null;
}

interface Skipped {
  path: string;
  reason: string;
}

export interface SourceListing {
  files: SourceFile[];
  /** Top-level folder names, so never-imported ones can be reported. */
  folders: string[];
  /** Folders the walk stopped at (too deep, or past the entry cap). */
  skipped?: Skipped[];
}

export interface ImportPlan {
  files: SourceFile[];
  skipped: Skipped[];
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

/** Run `fn` over `items` a few at a time. */
async function eachLimited<T>(items: T[], fn: (item: T, index: number) => Promise<void>) {
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      // oxlint-disable-next-line no-await-in-loop
      await fn(items[index], index);
    }
  };
  await Promise.all(Array.from({ length: IMPORT_CONCURRENCY }, worker));
}

interface Dir {
  path: string;
  rel: string;
}

// Breadth-first and capped, since the host listing follows symlinked folders.
async function walk(roots: Dir[], list: ListHostDir) {
  const files: SourceFile[] = [];
  const skipped: Skipped[] = [];
  let listed = 0;
  let level = roots;
  for (let depth = 1; level.length; depth++) {
    const found: { files: SourceFile[]; dirs: Dir[] }[] = [];
    // oxlint-disable-next-line no-await-in-loop
    await eachLimited(level, async (dir, i) => {
      if (listed > MAX_LISTED_ENTRIES) return;
      const entries = await listAll(dir.path, list);
      listed += entries.length;
      if (listed > MAX_LISTED_ENTRIES) {
        skipped.push({
          path: `${dir.rel}/`,
          reason: `over the ${MAX_LISTED_ENTRIES}-entry listing limit`,
        });
        return;
      }
      found[i] = {
        files: entries
          .filter((e) => e.type === "file")
          .map((e) => ({ path: `${dir.rel}/${e.name}`, bytes: e.bytes })),
        dirs: entries
          .filter((e) => e.type === "directory")
          .map((e) => ({ path: e.path, rel: `${dir.rel}/${e.name}` })),
      };
    });
    const next: Dir[] = [];
    for (const f of found) {
      if (!f) continue;
      files.push(...f.files);
      if (depth < MAX_WALK_DEPTH) next.push(...f.dirs);
      else
        skipped.push(
          ...f.dirs.map((d) => ({
            path: `${d.rel}/`,
            reason: `nested deeper than ${MAX_WALK_DEPTH} folders`,
          })),
        );
    }
    level = listed > MAX_LISTED_ENTRIES ? [] : next;
  }
  return { files, skipped };
}

/** List the allowlisted files under `folder`; other folders are never walked. */
export async function listDesignSystemSource(
  folder: string,
  list: ListHostDir,
): Promise<SourceListing> {
  const top = await listAll(folder, list);
  const files = top
    .filter((e) => e.type === "file" && IMPORT_FILES.includes(e.name))
    .map((e) => ({ path: e.name, bytes: e.bytes }));
  const dirs = top.filter((e) => e.type === "directory");
  const nested = await walk(
    dirs.filter((e) => IMPORT_FOLDERS.includes(e.name)).map((e) => ({ path: e.path, rel: e.name })),
    list,
  );
  return {
    files: [...files, ...nested.files],
    folders: dirs.map((e) => e.name),
    skipped: nested.skipped,
  };
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
    const bytes = file.bytes ?? 0;
    if (!reason && bytes > DS_ASSET_MAX_BYTES) {
      reason = `larger than ${DS_ASSET_MAX_BYTES / MB} MB`;
    } else if (!reason && plan.totalBytes + bytes > DS_DECK_MAX_BYTES) {
      reason = `over the ${DS_DECK_MAX_BYTES / MB} MB total`;
    }
    if (reason) {
      plan.skipped.push({ path: file.path, reason });
    } else {
      plan.files.push(file);
      plan.totalBytes += bytes;
    }
  }
  plan.skipped.push(...(source.skipped ?? []));
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

function decodedBytes({ content, encoding }: KitFile): number {
  if (encoding === "utf-8") return new TextEncoder().encode(content).length;
  return Math.floor((content.length * 3) / 4) - (content.match(/=*$/)?.[0].length ?? 0);
}

// Sizes are checked on the read content, since a listing size can be missing or stale.
async function copyOne(file: SourceFile, deps: ImportDeps, total: { bytes: number }) {
  const found = await deps.read(file.path);
  if (!found) throw new Error("not found");
  if (found.truncated) throw new Error("too long to import (the read was truncated)");
  const bytes = decodedBytes(found);
  if (bytes > DS_ASSET_MAX_BYTES) throw new Error(`larger than ${DS_ASSET_MAX_BYTES / MB} MB`);
  if (total.bytes + bytes > DS_DECK_MAX_BYTES) {
    throw new Error(`over the ${DS_DECK_MAX_BYTES / MB} MB total`);
  }
  total.bytes += bytes;
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
  const total = { bytes: 0 };
  let done = 0;
  await eachLimited(plan.files, async (file, index) => {
    try {
      await copyOne(file, deps, total);
    } catch (e) {
      failed.set(index, { path: file.path, message: e instanceof Error ? e.message : String(e) });
    }
    done += 1;
    deps.onProgress(done, plan.files.length);
  });
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
