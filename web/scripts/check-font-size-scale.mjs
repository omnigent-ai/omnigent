import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const sourceRoot = new URL("../src/", import.meta.url);
const sourceExtensions = new Set([".css", ".ts", ".tsx"]);
const arbitraryPixelText = /text-\[(?:\d+(?:\.\d+)?)px\]/g;

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(
    entries.map(async (entry) => {
      const entryPath = new URL(entry.name, directory);
      if (entry.isDirectory()) return sourceFiles(new URL(`${entry.name}/`, directory));
      return sourceExtensions.has(path.extname(entry.name)) ? [entryPath] : [];
    }),
  );
  return files.flat();
}

const violations = (
  await Promise.all(
    (await sourceFiles(sourceRoot)).map(async (file) => {
      const source = await readFile(file, "utf8");
      return [...source.matchAll(arbitraryPixelText)].map((match) => {
        const line = source.slice(0, match.index).split("\n").length;
        return `${path.relative(new URL("../", import.meta.url).pathname, file.pathname)}:${line}`;
      });
    }),
  )
).flat();

if (violations.length > 0) {
  console.error(
    "Arbitrary pixel text utilities are not allowed; add or reuse an @theme text token:",
  );
  for (const violation of violations) console.error(`  ${violation}`);
  process.exitCode = 1;
}
