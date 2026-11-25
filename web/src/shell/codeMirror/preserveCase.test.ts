import { describe, expect, it } from "vitest";
import { withCaseOf } from "./preserveCase";

describe("withCaseOf", () => {
  it.each([
    ["all caps stays all caps", "FOOBAR", "bazQux", "BAZQUX"],
    ["lower case stays lower case", "foobar", "BazQux", "bazqux"],
    ["a capitalised word stays capitalised", "Foobar", "bazQux", "BazQux"],
    ["a word starting lower keeps the rest as written", "fooBar", "BazQux", "bazQux"],
    ["hyphenated parts match part by part", "Foo-BAR", "baz-qux", "Baz-QUX"],
    ["underscored parts match part by part", "foo_BAR", "Baz_qux", "baz_QUX"],
    ["parts that don't line up use the whole word", "Foo-bar", "baz", "Baz"],
    ["digits and symbols count as both cases", "123", "baz", "BAZ"],
    ["non-Latin capitals are kept", "Ärger", "öl", "Öl"],
  ])("%s", (_case, matched, replacement, expected) => {
    expect(withCaseOf(matched, replacement)).toBe(expected);
  });

  it("leaves an empty match or replacement alone", () => {
    expect(withCaseOf("", "baz")).toBe("baz");
    expect(withCaseOf("FOO", "")).toBe("");
  });
});
