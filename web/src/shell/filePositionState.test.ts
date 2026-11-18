import { describe, expect, it } from "vitest";
import { citedFilePosition } from "./filePositionState";

describe("citedFilePosition", () => {
  it.each([
    ["no options", undefined, undefined],
    ["no line", { endLine: 9 }, undefined],
    ["a line and column", { line: 7, column: 3 }, { line: 7, column: 3 }],
    ["a zero line", { line: 0, endLine: 9 }, undefined],
    ["a fractional line", { line: 7.5 }, undefined],
    ["an invalid column and end", { line: 7, column: 0, endLine: Number.NaN }, { line: 7 }],
    ["a range", { line: 7, endLine: 9 }, { line: 7, endLine: 9 }],
    ["an end equal to its start", { line: 7, endLine: 7 }, { line: 7 }],
    ["an end before its start", { line: 7, endLine: 5 }, { line: 7 }],
  ])("reads %s", (_label, options, position) => {
    expect(citedFilePosition(options)).toStrictEqual(position);
  });
});
