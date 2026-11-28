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
    // Deseret letters lie outside the Basic Multilingual Plane: each is two UTF-16 units.
    ["a capital initial beyond the BMP capitalises", "\u{10400}\u{10428}", "word", "Word"],
    ["a capital initial capitalises one beyond the BMP", "Title", "\u{10428}x", "\u{10400}x"],
    ["a lower initial beyond the BMP lowers one", "\u{10428}\u{10400}", "\u{10400}X", "\u{10428}X"],
  ])("%s", (_case, matched, replacement, expected) => {
    expect(withCaseOf(matched, replacement)).toBe(expected);
  });

  it("leaves an empty match or replacement alone", () => {
    expect(withCaseOf("", "baz")).toBe("baz");
    expect(withCaseOf("FOO", "")).toBe("");
  });
});
