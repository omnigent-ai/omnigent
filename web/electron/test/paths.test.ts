import assert from "node:assert/strict";
import path from "node:path";
import { it } from "node:test";

import { pagePath, preloadPath } from "../src/paths.ts";

const PACKAGE_ROOT = path.join(import.meta.dirname, "..");

it("resolves pages under the package root", () => {
  assert.equal(pagePath("setup"), path.join(PACKAGE_ROOT, "setup", "index.html"));
  assert.equal(
    pagePath("overlay", "update-overlay.html"),
    path.join(PACKAGE_ROOT, "overlay", "update-overlay.html"),
  );
});

it("resolves preloads to their bundles in app/", () => {
  assert.equal(preloadPath("find_preload"), path.join(PACKAGE_ROOT, "app", "find_preload.js"));
});
