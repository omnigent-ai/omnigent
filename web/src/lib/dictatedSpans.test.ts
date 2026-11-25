import { describe, expect, it } from "vitest";
import {
  mapDictatedSpans,
  mapDictatedSpansAcrossDocuments,
  type DictatedSpan,
} from "./dictatedSpans";

describe("dictated spans", () => {
  it("shifts a span that follows an edit, and keeps one an edit lies beyond", () => {
    const spans: DictatedSpan[] = [{ from: 6, to: 12 }];
    expect(mapDictatedSpans(spans, { from: 0, to: 0, insert: "Intro " })).toEqual([
      { from: 12, to: 18 },
    ]);
    expect(mapDictatedSpans(spans, { from: 12, to: 12, insert: " more" })).toEqual(spans);
    expect(mapDictatedSpans(spans, { from: 6, to: 6, insert: "x" })).toEqual([{ from: 7, to: 13 }]);
  });

  it("drops a span an edit overlaps, so typed or deleted text is never discarded", () => {
    // Repro: "Draft spoken spoken", the second " spoken" is dictated, the user deletes it by hand.
    const dictated: DictatedSpan[] = [{ from: 12, to: 19 }];
    expect(
      mapDictatedSpansAcrossDocuments(dictated, "Draft spoken spoken", "Draft spoken"),
    ).toEqual([]);
  });

  it("maps a span across a quote appended after it, and across a deletion-only edit", () => {
    const dictated: DictatedSpan[] = [{ from: 5, to: 11 }];
    expect(
      mapDictatedSpansAcrossDocuments(dictated, "Draft spoken", "Draft spoken\nCARD\n"),
    ).toEqual(dictated);
    // An empty insert deletes text; the search it used to need never runs.
    expect(mapDictatedSpans(dictated, { from: 0, to: 2, insert: "" })).toEqual([
      { from: 3, to: 9 },
    ]);
  });
});
