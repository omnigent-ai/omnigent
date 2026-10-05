// Serves the synthetic design system in `fixtures/design-system/` the way the
// workspace file API does: text as utf-8, binaries as base64, null when missing.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { KitFile } from "@/shell/codeViewerHelpers";

export const DS_FIXTURE_DIR = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures/design-system",
);

export function readFixtureFile(rel: string): KitFile | null {
  const file = path.join(DS_FIXTURE_DIR, rel);
  if (!existsSync(file)) return null;
  const bytes = readFileSync(file);
  const text = bytes.toString("utf8");
  return text.includes("\u0000")
    ? { encoding: "base64", content: bytes.toString("base64"), bytes: bytes.length }
    : { encoding: "utf-8", content: text, bytes: bytes.length };
}

export const readFixture = async (rel: string) => readFixtureFile(rel);

/** A fixture folder's entries the way the directory listing names them; empty when missing. */
export function listFixtureDir(rel: string): { name: string; type: "file" | "directory" }[] {
  const dir = path.join(DS_FIXTURE_DIR, rel);
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true }).map((e) => ({
    name: e.name,
    type: e.isDirectory() ? "directory" : "file",
  }));
}
