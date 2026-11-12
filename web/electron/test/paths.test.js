"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { it } = require("node:test");

const { pagePath, preloadPath } = require("../src/paths");

const PACKAGE_ROOT = path.join(__dirname, "..");

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
