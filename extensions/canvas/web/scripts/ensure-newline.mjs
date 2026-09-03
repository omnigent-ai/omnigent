import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

for (const name of ["extension.js", "extension.css"]) {
  const path = resolve(import.meta.dirname, "../../src/omnigent_canvas/dist", name);
  const content = await readFile(path, "utf8");
  if (!content.endsWith("\n")) await writeFile(path, `${content}\n`);
}
