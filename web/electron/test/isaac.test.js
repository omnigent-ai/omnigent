"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { resolveIsaacPath } = require("../src/isaac");

describe("isaac binary resolution", () => {
  it("prefers PATH, then well-known locations, and fails closed", () => {
    assert.equal(
      resolveIsaacPath({
        whichIsaac: () => "/from/path/isaac",
        isExecutableFile: (value) => value === "/from/path/isaac",
        candidatePaths: () => ["/usr/local/bin/isaac"],
      }),
      "/from/path/isaac",
    );
    assert.equal(
      resolveIsaacPath({
        whichIsaac: () => null,
        isExecutableFile: (value) => value === "/usr/local/bin/isaac",
        candidatePaths: () => ["/usr/local/bin/isaac"],
      }),
      "/usr/local/bin/isaac",
    );
    assert.equal(
      resolveIsaacPath({
        whichIsaac: () => null,
        isExecutableFile: () => false,
        candidatePaths: () => ["/usr/local/bin/isaac"],
      }),
      null,
    );
  });
});
