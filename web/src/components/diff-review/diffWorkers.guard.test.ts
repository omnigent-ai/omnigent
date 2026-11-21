// The diff workers run without a DOM, so nothing they import, however indirectly, may load React
// or a component; a model shared with the review imports only the tree's DOM-free modules.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const WORKERS = [
  "components/diff-review/fileDiffWorker.ts",
  "components/diff-review/markdownDiffWorker.ts",
];

const withoutComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");

/** The source file an import names, or null for a package. */
function resolveImport(from: string, specifier: string): string | null {
  const base = specifier.startsWith("@/")
    ? path.join(SRC, specifier.slice(2))
    : specifier.startsWith(".")
      ? path.resolve(path.dirname(from), specifier)
      : null;
  if (!base) return null;
  const candidates = [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts")];
  return candidates.find((file) => fs.existsSync(file) && fs.statSync(file).isFile()) ?? null;
}

/** Every value import reachable from `entry`: packages by name, source files by path. */
function importGraph(entry: string): { files: Set<string>; packages: Set<string> } {
  const files = new Set<string>();
  const packages = new Set<string>();
  const pending = [entry];
  while (pending.length) {
    const file = pending.pop()!;
    if (files.has(file)) continue;
    files.add(file);
    const source = withoutComments(fs.readFileSync(file, "utf8"));
    // Type-only imports vanish from the bundle, so they don't count.
    for (const match of source.matchAll(
      /^\s*(import|export)\s+(?!type\s)[^;]*?from\s+"([^"]+)"/gms,
    )) {
      const specifier = match[2]!;
      const resolved = resolveImport(file, specifier);
      if (resolved) pending.push(resolved);
      else if (!specifier.startsWith(".") && !specifier.startsWith("@/")) packages.add(specifier);
    }
  }
  return { files, packages };
}

describe("diff workers", () => {
  it.each(WORKERS)("%s loads no React and no component", (worker) => {
    const { files, packages } = importGraph(path.join(SRC, worker));
    expect([...packages].filter((name) => /^react(-dom)?(\/|$)/.test(name))).toEqual([]);
    expect(
      [...files].filter((file) => file.endsWith(".tsx")).map((file) => path.relative(SRC, file)),
    ).toEqual([]);
  });
});
