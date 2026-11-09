// Resolve catalog-only inputs with the same Pi module used by its native picker.
const fs = require("node:fs");
const { pathToFileURL } = require("node:url");

async function main() {
  const { resolveModelScopeFromModels } = await import(
    pathToFileURL(process.argv[2]).href
  );
  const { patterns, models } = JSON.parse(fs.readFileSync(0, "utf8"));
  const { scopedModels } = resolveModelScopeFromModels(patterns, models);
  process.stdout.write(
    JSON.stringify(scopedModels.map(({ model }) => models.indexOf(model))),
  );
}

main().catch(() => {
  process.exitCode = 1;
});
