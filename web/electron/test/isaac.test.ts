import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { resolveIsaacPath } from "../src/isaac.ts";

describe("isaac binary resolution", () => {
  it("prefers PATH, then well-known locations, and fails closed", () => {
    assert.equal(
      resolveIsaacPath({
        whichIsaac: () => "/from/path/isaac",
        isExecutableFile: (value: string) => value === "/from/path/isaac",
        candidatePaths: () => ["/usr/local/bin/isaac"],
      }),
      "/from/path/isaac",
    );
    assert.equal(
      resolveIsaacPath({
        whichIsaac: () => null,
        isExecutableFile: (value: string) => value === "/usr/local/bin/isaac",
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
