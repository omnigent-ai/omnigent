"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { DEFAULT_BROWSER_PROFILE_ID, browserProfilePartition } = require("../src/browserProfiles");

describe("browserProfilePartition", () => {
  it("maps a profile id to a persistent partition", () => {
    assert.equal(
      browserProfilePartition(DEFAULT_BROWSER_PROFILE_ID),
      "persist:omnigent-browser-default",
    );
    assert.equal(browserProfilePartition("work-2"), "persist:omnigent-browser-work-2");
  });

  it("rejects ids that could escape or collide in the partition namespace", () => {
    for (const id of ["", "Work", "a/b", "../x", "-lead", "a b", "x".repeat(65), null, 7]) {
      assert.throws(() => browserProfilePartition(id), /Invalid browser profile id/);
    }
  });
});
