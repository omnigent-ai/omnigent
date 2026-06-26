// Regression guard for the workspace-chrome CSS injection in src/main.js, run
// with `node --test` (no extra deps). The bug: a `pathname.startsWith(
// WORKSPACE_UI_PATH)` guard around insertCSS silently skipped injection when
// the loaded URL didn't match the mount path (auth redirects, path variants),
// leaving the Databricks workspace chrome visible. The CSS targets
// `.omnigent-app`, which only exists in the workspace-embedded build, so
// injecting unconditionally is a harmless no-op on standalone servers. This
// guard fails if the path condition is reintroduced.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const mainSource = readFileSync(path.join(__dirname, "../src/main.js"), "utf8");

describe("workspace chrome CSS injection (src/main.js)", () => {
  it("injects WORKSPACE_CHROME_HIDE_CSS from the did-finish-load handler", () => {
    const handler = mainSource.match(/did-finish-load"\s*,\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*\)\s*;/);
    assert.ok(handler, "did-finish-load handler not found in main.js");
    assert.match(handler[1], /insertCSS\(WORKSPACE_CHROME_HIDE_CSS\)/);
  });

  it("does not gate injection behind a WORKSPACE_UI_PATH check", () => {
    const handler = mainSource.match(/did-finish-load"\s*,\s*\(\)\s*=>\s*\{([\s\S]*?)\}\s*\)\s*;/);
    assert.ok(handler, "did-finish-load handler not found in main.js");
    assert.doesNotMatch(
      handler[1],
      /WORKSPACE_UI_PATH/,
      "path guard reintroduced — CSS injection must be unconditional",
    );
  });
});
