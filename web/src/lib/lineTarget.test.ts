import { describe, expect, it } from "vitest";

import { parseLineTarget } from "./lineTarget";

describe("parseLineTarget", () => {
  it.each([
    ["12", { kind: "target", line: 12 }],
    [" 12:8 ", { kind: "target", line: 12, column: 8 }],
    ["12, 8", { kind: "target", line: 12, column: 8 }],
    ["0", { kind: "target", line: 1 }],
    ["900", { kind: "target", line: 340 }],
    ["12:0", { kind: "target", line: 12, column: 1 }],
    ["", { kind: "empty" }],
    ["   ", { kind: "empty" }],
    ["-3", { kind: "invalid" }],
    ["twelve", { kind: "invalid" }],
    ["12:", { kind: "invalid" }],
    ["1.5", { kind: "invalid" }],
  ])("reads %j", (text, expected) => {
    expect(parseLineTarget(text, 340)).toEqual(expected);
  });

  it("keeps an empty file's target on its only line", () => {
    expect(parseLineTarget("5", 0)).toEqual({ kind: "target", line: 1 });
  });
});
