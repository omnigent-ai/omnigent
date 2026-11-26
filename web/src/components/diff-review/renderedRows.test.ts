import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  containsAcrossShadowRoots,
  currentSidePosition,
  currentSideRow,
  isCurrentSideRow,
  readRenderedSelection,
  revealColumn,
  sideRow,
  textOffsetWithin,
  textRange,
  topCurrentSideRow,
} from "./renderedRows";

// Rows as the diff renders them: tokens in spans inside `[data-content]`.
function row(line: number, type: string, ...tokens: string[]) {
  const tokenSpans = tokens.map((token) => `<span>${token}</span>`).join("");
  return `<div data-line="${line}" data-line-type="${type}"><span data-diff-span>${tokenSpans}</span></div>`;
}

function renderedFile(html: string) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = html;
  return root;
}

// jsdom can't select inside shadow trees; Chromium reports them through getComposedRanges.
function selectComposed(range: Range) {
  vi.spyOn(document, "getSelection").mockReturnValue({
    rangeCount: 1,
    getComposedRanges: () => [range],
    getRangeAt: () => range,
  } as unknown as Selection);
}

afterEach(() => {
  vi.restoreAllMocks();
  document.getSelection()?.removeAllRanges();
  document.body.replaceChildren();
});

const unified = `<div data-content>${[
  row(1, "context", "def", " handler():"),
  row(2, "change-deletion", "    return", " 'old'"),
  row(2, "change-addition", "    return", " 'needle'"),
].join("")}</div>`;

const split = `<div data-deletions><div data-content>${row(4, "context", "same")}${row(
  5,
  "change-deletion",
  "gone",
)}</div></div><div data-additions><div data-content>${row(4, "context", "same")}${row(
  5,
  "change-addition",
  "new",
)}</div></div>`;

describe("current-version rows", () => {
  it("treats additions and unified context as the current version", () => {
    const root = renderedFile(unified);
    const rows = [...root.querySelectorAll("[data-line]")];
    expect(rows.map(isCurrentSideRow)).toEqual([true, false, true]);
    expect(currentSideRow(root, 2)?.textContent).toBe("    return 'needle'");
  });

  it("leaves out the deletions column in split layout", () => {
    const root = renderedFile(split);
    expect(currentSideRow(root, 4)?.closest("[data-additions]")).not.toBeNull();
    expect(currentSideRow(root, 5)?.textContent).toBe("new");
    expect(currentSideRow(root, 9)).toBeUndefined();
  });

  it("finds the line and column at a point in a row's text", () => {
    const root = renderedFile(unified);
    const addition = currentSideRow(root, 2)!;
    const token = addition.querySelectorAll("span span")[1]!.firstChild!;
    expect(currentSidePosition(token, 2)).toEqual({ line: 2, column: 12 });
    const deletion = root.querySelector('[data-line-type="change-deletion"] span span')!;
    expect(currentSidePosition(deletion.firstChild!, 0)).toBeUndefined();
  });

  it("addresses columns across a row's tokens", () => {
    const root = renderedFile(unified);
    const range = textRange(currentSideRow(root, 2)!, 12, 18)!;
    expect(range.toString()).toBe("needle");
    expect(textRange(currentSideRow(root, 2)!, 12, 99)).toBeUndefined();
    expect(textRange(currentSideRow(root, 2)!, 5, 5)).toBeUndefined();
  });
});

describe("textOffsetWithin", () => {
  it("counts a text position the way textRange does, across split text", () => {
    const element = document.createElement("p");
    element.innerHTML = "Alpha <em>beta</em> gamma";
    const beta = element.querySelector("em")!.firstChild!;
    const offset = textOffsetWithin(element, beta, 2)!;
    expect(offset).toBe(8);
    expect(textRange(element, offset, offset + 2)!.toString()).toBe("ta");
    expect(textOffsetWithin(element, element.lastChild!, 6)).toBe(16);
  });

  it("names no offset for an element position, another element's text, or past the text", () => {
    const element = document.createElement("p");
    element.innerHTML = "Alpha <em>beta</em>";
    const beta = element.querySelector("em")!.firstChild!;
    expect(textOffsetWithin(element, element, 1)).toBeUndefined();
    expect(textOffsetWithin(element, document.createTextNode("other"), 1)).toBeUndefined();
    expect(textOffsetWithin(element, beta, 5)).toBeUndefined();
    expect(textOffsetWithin(element, beta, -1)).toBeUndefined();
  });
});

describe("readRenderedSelection", () => {
  it("reads a selection within one file as current-version positions", () => {
    const root = renderedFile(unified);
    const roots = new Map([["diff:a.py", root]]);
    selectComposed(textRange(currentSideRow(root, 2)!, 12, 18)!);
    expect(readRenderedSelection(roots)).toMatchObject({
      itemId: "diff:a.py",
      start: { line: 2, column: 12 },
      end: { line: 2, column: 18 },
      collapsed: false,
    });
  });

  it("ignores selections in deleted lines or outside the review", () => {
    const root = renderedFile(unified);
    const roots = new Map([["diff:a.py", root]]);
    const deleted = root.querySelector('[data-line-type="change-deletion"]')!;
    selectComposed(textRange(deleted, 0, 4)!);
    expect(readRenderedSelection(roots)).toBeUndefined();
    const outside = document.createElement("p");
    outside.textContent = "elsewhere";
    document.body.append(outside);
    selectComposed(textRange(outside, 0, 4)!);
    expect(readRenderedSelection(roots)).toBeUndefined();
  });
});

function placeAt(element: Element, top: number, bottom: number, left = 0, right = 100) {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue(
    DOMRect.fromRect({ x: left, y: top, width: right - left, height: bottom - top }),
  );
}

describe("topCurrentSideRow", () => {
  it("finds the first current-version row still below the viewport's top", () => {
    const root = renderedFile(
      `<div data-content>${row(4, "change-deletion", "old")}${row(4, "context", "above")}${row(5, "change-deletion", "gone")}${row(5, "change-addition", "new")}</div>`,
    );
    const rows = [...root.querySelectorAll("[data-line]")];
    rows.forEach((element, index) => placeAt(element, index * 20, index * 20 + 20));
    expect(topCurrentSideRow(root, 25)).toEqual({ line: 4, top: 20 });
    expect(topCurrentSideRow(root, 45)).toEqual({ line: 5, top: 60 });
    expect(topCurrentSideRow(root, 100)).toBeUndefined();
  });
});

describe("revealColumn", () => {
  // jsdom doesn't lay out text, so ranges report where the test puts them.
  let rangeBounds = new DOMRect();
  beforeEach(() => {
    Range.prototype.getBoundingClientRect = () => rangeBounds;
  });
  afterEach(() => {
    delete (Range.prototype as Partial<Range>).getBoundingClientRect;
  });

  function longRow() {
    const root = renderedFile(
      `<div data-scroller style="overflow-x: auto">${row(1, "context", "x".repeat(300))}</div>`,
    );
    const scroller = root.querySelector("[data-scroller]")!;
    Object.defineProperty(scroller, "scrollWidth", { value: 3000 });
    Object.defineProperty(scroller, "clientWidth", { value: 400 });
    placeAt(scroller, 0, 20, 0, 400);
    return { scroller, line: root.querySelector("[data-line]")! };
  }

  it("centers an off-screen column in the row's horizontal scroller", () => {
    const { scroller, line } = longRow();
    rangeBounds = DOMRect.fromRect({ x: 2000, width: 10 });
    revealColumn(line, 200);
    expect(scroller.scrollLeft).toBe(1800);
  });

  it("leaves a visible column where it is", () => {
    const { scroller, line } = longRow();
    rangeBounds = DOMRect.fromRect({ x: 100, width: 10 });
    revealColumn(line, 10);
    expect(scroller.scrollLeft).toBe(0);
  });
});

describe("rows by side", () => {
  // Pierre labels an unchanged unified row with its old line number as data-alt-line.
  const unifiedWithOldNumbers = `<div data-content>${[
    `<div data-line="7" data-alt-line="5" data-line-type="context">same</div>`,
    row(6, "change-deletion", "gone"),
    row(8, "change-addition", "new"),
  ].join("")}</div>`;

  it("finds a deleted line and an unchanged line by its old number in the unified layout", () => {
    const root = renderedFile(unifiedWithOldNumbers);
    expect(sideRow(root, 6, "deletions")?.textContent).toBe("gone");
    expect(sideRow(root, 5, "deletions")?.textContent).toBe("same");
    expect(sideRow(root, 7, "deletions")).toBeUndefined();
    expect(sideRow(root, 7, "additions")?.textContent).toBe("same");
    expect(sideRow(root, 8, "additions")?.textContent).toBe("new");
  });

  it("finds old-version lines in the deletions column of the split layout", () => {
    const root = renderedFile(split);
    expect(sideRow(root, 4, "deletions")?.closest("[data-deletions]")).not.toBeNull();
    expect(sideRow(root, 5, "deletions")?.textContent).toBe("gone");
    expect(sideRow(root, 5, "additions")?.textContent).toBe("new");
  });
});

describe("containsAcrossShadowRoots", () => {
  it("finds a node inside a descendant's shadow root, and nothing outside the container", () => {
    const shell = document.createElement("div");
    const host = document.createElement("div");
    shell.append(host);
    const shadowRow = document.createElement("div");
    shadowRow.textContent = "needle";
    host.attachShadow({ mode: "open" }).append(shadowRow);
    const outside = document.createElement("div");
    document.body.append(shell, outside);
    expect(shell.contains(shadowRow.firstChild)).toBe(false);
    expect(containsAcrossShadowRoots(shell, shadowRow.firstChild)).toBe(true);
    expect(containsAcrossShadowRoots(shell, outside)).toBe(false);
    expect(containsAcrossShadowRoots(shell, null)).toBe(false);
  });
});
