const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const packageConfig = require("../package.json");

describe("macOS package metadata", () => {
  it("declares why Omnigent adds images to the photo library", () => {
    const description = packageConfig.build.mac.extendInfo.NSPhotoLibraryAddUsageDescription;

    assert.equal(typeof description, "string");
    assert.notEqual(description.trim(), "");
  });
});
