import { EditorSelection, EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";
import {
  documentOf,
  preferredLineBreak,
  rawDocument,
  rawDocumentOf,
  RawOffsetMap,
  rawSelectionText,
  savedTextOf,
} from "./rawOffsets";

/** Every raw offset that isn't inside a "\r\n" pair, with the document position CodeMirror gives it. */
function expectedPositions(raw: string): [number, number][] {
  const doc = EditorState.create({ doc: raw }).doc;
  const pairs: [number, number][] = [];
  let position = 0;
  for (let offset = 0; offset <= raw.length; offset += 1) {
    if (raw[offset - 1] === "\r" && raw[offset] === "\n") continue;
    pairs.push([offset, position]);
    if (offset < raw.length) position += 1;
  }
  expect(position).toBe(doc.length);
  return pairs;
}

describe("RawOffsetMap", () => {
  it.each([
    ["LF", "one\ntwo\nthree"],
    ["CRLF", "one\r\ntwo\r\nthree\r\n"],
    ["mixed line breaks", "a\r\nb\nc\rd\r\n\r\ne"],
    ["no line breaks", "alpha"],
    ["an empty file", ""],
  ])("round-trips every offset in a file with %s", (_label, raw) => {
    const map = RawOffsetMap.of(raw);
    for (const [offset, position] of expectedPositions(raw)) {
      expect(map.toDocument(offset)).toBe(position);
      expect(map.toRaw(position)).toBe(offset);
    }
  });

  it("maps an offset between \\r and \\n to the line break", () => {
    const map = RawOffsetMap.of("ab\r\ncd");
    expect(map.toDocument(3)).toBe(2);
  });
});

describe("rawSelectionText", () => {
  const raw = "first\r\nsecond\nthird\r\nlast";
  const state = EditorState.create({
    doc: raw,
    extensions: [
      rawDocument.init(() => rawDocumentOf(raw)),
      EditorState.allowMultipleSelections.of(true),
    ],
  });

  it("keeps each line break the file has", () => {
    const selected = state.update({
      selection: EditorSelection.range(2, state.doc.line(4).from + 2),
    }).state;
    expect(rawSelectionText(selected)).toBe("rst\r\nsecond\nthird\r\nla");
  });

  it("joins several selections with line breaks and skips empty ones", () => {
    const selected = state.update({
      selection: EditorSelection.create([
        EditorSelection.range(0, 5),
        EditorSelection.cursor(8),
        EditorSelection.range(state.doc.line(4).from, state.doc.length),
      ]),
    }).state;
    expect(rawSelectionText(selected)).toBe("first\nlast");
  });
});

describe("preferredLineBreak and savedTextOf", () => {
  it.each([
    ["CRLF", "a\r\nb\r\nc", "\r\n"],
    ["LF", "a\nb", "\n"],
    ["mostly CRLF", "a\r\nb\r\nc\nd", "\r\n"],
    ["evenly mixed", "a\r\nb\nc", "\n"],
    ["no breaks", "abc", "\n"],
  ])("saves a %s file with its usual break", (_label, raw, expected) => {
    expect(preferredLineBreak(raw)).toBe(expected);
  });

  it("writes an unedited CRLF file back byte for byte", () => {
    const raw = "first\r\nsecond\r\n";
    expect(savedTextOf(documentOf(raw), preferredLineBreak(raw))).toBe(raw);
  });

  it("splits raw content into the same lines CodeMirror does", () => {
    const raw = "a\r\nb\nc\rd";
    expect(documentOf(raw).eq(EditorState.create({ doc: raw }).doc)).toBe(true);
  });
});
