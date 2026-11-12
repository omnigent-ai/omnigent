"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const os = require("node:os");
const path = require("node:path");
const { it } = require("node:test");

const { buildDesignModeScript } = require("../src/designModeScript.ts");
const { buildBrowserActionScript } = require("../src/browserActionScript.ts");

const SRC_DIR = path.join(__dirname, "..", "src");

function temporaryOutDir(t) {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-electron-build-"));
  t.after(() => fs.rmSync(outDir, { recursive: true, force: true }));
  return outDir;
}

it("bundled injected-script builders emit the same scripts as source", async (t) => {
  const { bundleMainProcess } = await import("../scripts/build.mjs");
  const outDir = temporaryOutDir(t);
  const entry = path.join(outDir, "builders.js");
  fs.writeFileSync(
    entry,
    [
      `const { buildDesignModeScript } = require(${JSON.stringify(path.join(SRC_DIR, "designModeScript.ts"))});`,
      `const { buildBrowserActionScript } = require(${JSON.stringify(path.join(SRC_DIR, "browserActionScript.ts"))});`,
      "module.exports = { buildDesignModeScript, buildBrowserActionScript };",
    ].join("\n"),
  );

  await bundleMainProcess({ entry, outDir, fileName: "builders.cjs" });
  const bundled = require(path.join(outDir, "builders.cjs"));

  assert.equal(bundled.buildDesignModeScript("nonce-1"), buildDesignModeScript("nonce-1"));
  for (const [action, args] of [
    ["snapshot", {}],
    ["click", { ref: 3, snapshot_id: "snap-1" }],
    ["click", { selector: "#save" }],
    ["type", { ref: 2, text: 'Ada "quoted" `tick` ${x}' }],
  ]) {
    assert.equal(
      bundled.buildBrowserActionScript(action, args),
      buildBrowserActionScript(action, args),
    );
  }
});

it("bundled main-process code applies test overrides only in an unpackaged app", async (t) => {
  const { bundleMainProcess } = await import("../scripts/build.mjs");
  const { registerTestOverride } = require("../src/testOverrides");
  const outDir = temporaryOutDir(t);
  const entry = path.join(outDir, "overrides.js");
  fs.writeFileSync(
    entry,
    [
      `const { withTestOverrides } = require(${JSON.stringify(path.join(SRC_DIR, "testOverrides.js"))});`,
      `module.exports = withTestOverrides("url", require(${JSON.stringify(path.join(SRC_DIR, "url.ts"))}));`,
    ].join("\n"),
  );
  await bundleMainProcess({ entry, outDir, fileName: "overrides.cjs" });
  const bundlePath = path.join(outDir, "overrides.cjs");

  const originalRequire = Module.prototype.require;
  t.after(() => {
    Module.prototype.require = originalRequire;
    delete globalThis.omnigentTestOverrides;
  });
  // Each load gets its own copy so the bundle evaluates again.
  const requireBundle = (isPackaged) => {
    Module.prototype.require = function (id, ...args) {
      if (id === "electron") return { app: { isPackaged } };
      return originalRequire.call(this, id, ...args);
    };
    const copy = path.join(outDir, `overrides-${isPackaged ? "packaged" : "unpackaged"}.cjs`);
    fs.copyFileSync(bundlePath, copy);
    return require(copy);
  };
  registerTestOverride("url", () => ({ isLoopbackServerUrl: () => "overridden" }));

  assert.equal(requireBundle(false).isLoopbackServerUrl("https://example.com"), "overridden");
  assert.equal(requireBundle(true).isLoopbackServerUrl("https://example.com"), false);
});

it("bundles each preload as one file that requires only electron", async (t) => {
  const { bundlePreload, listPreloadFiles } = await import("../scripts/build.mjs");
  const outDir = temporaryOutDir(t);
  const preloadFiles = await listPreloadFiles();
  assert.ok(preloadFiles.includes("preload.js"));
  assert.ok(preloadFiles.includes("find_preload.js"));

  for (const fileName of preloadFiles) {
    // oxlint-disable-next-line no-await-in-loop -- Builds share one output directory.
    await bundlePreload({ fileName, outDir });
    const bundle = fs.readFileSync(path.join(outDir, fileName), "utf8");
    const requires = new Set(
      [...bundle.matchAll(/\brequire\(\s*["'`]([^"'`]+)/g)].map((m) => m[1]),
    );
    assert.ok(
      [...requires].every((specifier) => specifier === "electron"),
      fileName,
    );
  }
  const outputs = fs.readdirSync(outDir).filter((file) => file.endsWith(".js"));
  assert.deepEqual(outputs.sort(), [...preloadFiles].sort());
});
