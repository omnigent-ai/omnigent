import { afterEach, describe, expect, it, vi } from "vitest";
import { forgetSkippedGroupSizes } from "./useSectionGroupSizes";

/** A group whose first section the browser skips, or lays out. */
function group(skipped: boolean, visibility = "", size = "auto 300px"): HTMLElement {
  const element = document.createElement("div");
  const section = element.appendChild(document.createElement("div"));
  section.append("text");
  section.checkVisibility = () => !skipped;
  element.style.contentVisibility = visibility;
  element.style.containIntrinsicSize = size;
  return element;
}

afterEach(() => vi.restoreAllMocks());

describe("forgetSkippedGroupSizes", () => {
  it("passes only skipped groups through hidden, without an auto size, in one style pass", () => {
    const skipped = [group(true), group(true, "auto", "auto 40px")];
    const visible = group(false);
    const seen: string[][] = [];
    vi.spyOn(window, "getComputedStyle").mockImplementation(() => {
      seen.push(
        [...skipped, visible].map(
          (each) => `${each.style.contentVisibility} ${each.style.containIntrinsicSize}`,
        ),
      );
      return {} as CSSStyleDeclaration;
    });
    expect(forgetSkippedGroupSizes([skipped[0]!, visible, skipped[1]!])).toBe(2);
    expect(seen).toEqual([["hidden 300px", "hidden 40px", " auto 300px"]]);
    // Each is back to its own styles.
    expect(skipped.map((each) => each.style.contentVisibility)).toEqual(["", "auto"]);
    expect(skipped.map((each) => each.style.containIntrinsicSize)).toEqual([
      "auto 300px",
      "auto 40px",
    ]);
    expect(visible.style.contentVisibility).toBe("");
  });

  it("keeps the groups' nodes, so ranges into them still hold", () => {
    const skipped = group(true);
    const text = skipped.firstElementChild!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 1);
    range.setEnd(text, 3);
    forgetSkippedGroupSizes([skipped]);
    expect(skipped.firstElementChild!.firstChild).toBe(text);
    expect(range.toString()).toBe("ex");
  });

  it("puts every group back even when the style pass fails", () => {
    const skipped = [group(true), group(true)];
    vi.spyOn(window, "getComputedStyle").mockImplementation(() => {
      throw new Error("style pass failed");
    });
    expect(() => forgetSkippedGroupSizes(skipped)).toThrow("style pass failed");
    expect(skipped.map((each) => each.style.contentVisibility)).toEqual(["", ""]);
    expect(skipped.map((each) => each.style.containIntrinsicSize)).toEqual([
      "auto 300px",
      "auto 300px",
    ]);
  });

  it("touches nothing where nothing is skipped, or the browser can't say", () => {
    const style = vi.spyOn(window, "getComputedStyle");
    const unknown = document.createElement("div");
    unknown.append(document.createElement("div"));
    expect(forgetSkippedGroupSizes([group(false), unknown, document.createElement("div")])).toBe(0);
    expect(style).not.toHaveBeenCalled();
  });
});
