import { describe, expect, it } from "vitest";
import type { ComposerTextTarget } from "@/components/composer/engine/composerInput";
import {
  discardDictatedSpans,
  mapDictatedSpans,
  mapDictatedSpansAcrossDocuments,
  writeDictatedEdit,
  type DictatedSpan,
} from "./dictatedSpans";

const CARD = "\uFDD0card\uFDD1";

interface FakeTarget {
  value: string;
  revealCaret: () => void;
  insertText: (text: string, start: number, end: number) => boolean;
}

/** A plain input with a value, as the composer's target sees it. */
function plainTarget(value: string): FakeTarget {
  const target: FakeTarget = {
    value,
    revealCaret: () => {},
    insertText(text: string, start: number, end: number) {
      target.value = target.value.slice(0, start) + text + target.value.slice(end);
      return true;
    },
  };
  return target;
}

/** The fake as the composer's target type, which the production functions take. */
const asTarget = (target: FakeTarget) => target as unknown as ComposerTextTarget;

/** An editor that moves a write made on a card's line to the line after the card, as it does. */
function cardTarget(value: string): FakeTarget {
  const target = plainTarget(value);
  target.insertText = (text: string, start: number, end: number) => {
    if (start === 0 && end === 0 && target.value.startsWith(CARD)) {
      target.value = `${CARD}\n${text}${target.value.slice(CARD.length + 1)}`;
      return true;
    }
    target.value = target.value.slice(0, start) + text + target.value.slice(end);
    return true;
  };
  return target;
}

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
    // A deletion-only edit (empty insert) shifts the following span back.
    expect(mapDictatedSpans(dictated, { from: 0, to: 2, insert: "" })).toEqual([
      { from: 3, to: 9 },
    ]);
  });

  it("follows a write the editor normalized, so a later partial replaces the right text", () => {
    // The hook asked for "text " above the card; the editor kept it on the line after the card.
    const target = cardTarget(`${CARD}\n`);
    let spans = writeDictatedEdit(asTarget(target), [], { from: 0, to: 0, insert: "text " });
    expect(spans).toEqual([{ from: CARD.length + 1, to: CARD.length + 6 }]);
    // A revised partial replaces the span it owns, at its own location.
    spans = writeDictatedEdit(asTarget(target), spans, {
      from: CARD.length + 1,
      to: CARD.length + 6,
      insert: "text more",
    });
    expect(target.value).toBe(`${CARD}\ntext more`);
    expect(spans).toEqual([{ from: CARD.length + 1, to: CARD.length + 10 }]);
  });

  it("an empty write clears the interim it replaced, and owns nothing", () => {
    const target = plainTarget("Draft spoken");
    const spans = writeDictatedEdit(asTarget(target), [], { from: 6, to: 6, insert: "spoken" });
    expect(target.value).toBe("Draft spokenspoken");
    expect(
      writeDictatedEdit(asTarget(target), spans, { from: 6, to: 6 + "spoken".length, insert: "" }),
    ).toEqual([]);
    expect(target.value).toBe("Draft spoken");
  });

  it("discards several spans, last first, and leaves the text between them", () => {
    const target = plainTarget("Keep one Keep two");
    discardDictatedSpans(asTarget(target), [
      { from: 5, to: 8 },
      { from: 14, to: 17 },
    ]);
    expect(target.value).toBe("Keep  Keep ");
  });

  it("drops a span an insert lands strictly inside, and owns only the new text", () => {
    const target = plainTarget("abcdefghij");
    const spans = writeDictatedEdit(asTarget(target), [{ from: 2, to: 8 }], {
      from: 4,
      to: 4,
      insert: "x",
    });
    expect(target.value).toBe("abcdxefghij");
    expect(spans).toEqual([{ from: 4, to: 5 }]);
  });
});
