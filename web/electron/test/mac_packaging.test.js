const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

describe("macOS packaging", () => {
  it("opts out of camera housing compatibility mode", () => {
    assert.equal(
      packageConfig.build.mac.extendInfo.NSPrefersDisplaySafeAreaCompatibilityMode,
      false,
    );
  });
});
