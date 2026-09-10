import { describe, expect, it } from "vitest";
import { insertTextAtCaret } from "./composerPaste";

describe("insertTextAtCaret", () => {
  it("inserts into an empty draft", () => {
    expect(insertTextAtCaret("", 0, 0, "hello")).toEqual({ next: "hello", caret: 5 });
  });

  it("inserts at the caret mid-string, leaving both sides intact", () => {
    expect(insertTextAtCaret("abcdef", 3, 3, "XYZ")).toEqual({ next: "abcXYZdef", caret: 6 });
  });

  it("replaces a non-empty selection with the pasted text", () => {
    expect(insertTextAtCaret("abcdef", 2, 4, "XYZ")).toEqual({ next: "abXYZef", caret: 5 });
  });

  it("clamps a caret index below 0 to the start of the draft", () => {
    expect(insertTextAtCaret("abc", -5, -5, "X")).toEqual({ next: "Xabc", caret: 1 });
  });

  it("clamps a caret index past the end to the draft's length", () => {
    expect(insertTextAtCaret("abc", 99, 99, "X")).toEqual({ next: "abcX", caret: 4 });
  });
});
