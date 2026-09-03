import { readdirSync, readFileSync } from "node:fs";
import { extname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = `${directory}/${entry.name}`;
    if (entry.isDirectory()) return sourceFiles(path);
    return [".ts", ".tsx"].includes(extname(entry.name)) ? [path] : [];
  });
}

describe("DPIA palette guard", () => {
  it("keeps DPIA page and library styling free of hardcoded hex colours", () => {
    const directories = [
      resolve(process.cwd(), "src/pages/dpia"),
      resolve(process.cwd(), "src/lib/dpia"),
    ];
    const violations = directories
      .flatMap(sourceFiles)
      .filter((path) => !path.endsWith("DpiaPrintPack.tsx"))
      .flatMap((path) => {
        const matches = readFileSync(path, "utf8").match(/#[\da-f]{3,8}\b/gi) ?? [];
        return matches.map((value) => `${path}: ${value}`);
      });
    expect(violations).toEqual([]);
  });
});
