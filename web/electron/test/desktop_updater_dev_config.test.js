"use strict";

// The development update config, parsed with js-yaml. The updater itself is
// tested in desktop_updater.test.ts.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const yaml = require("js-yaml");

describe("desktop_updater — development update config", () => {
  it("uses the production update endpoint in development", () => {
    const devConfig = yaml.load(
      fs.readFileSync(path.join(__dirname, "../dev-app-update.yml"), "utf8"),
    );
    const packageConfig = require("../package.json");

    assert.equal(devConfig.url, packageConfig.build.publish[0].url);
    assert.match(devConfig.url, /^https:\/\//);
  });
});
