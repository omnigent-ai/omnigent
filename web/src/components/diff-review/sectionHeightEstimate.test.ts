import { describe, expect, it } from "vitest";
import { computeMarkdownDiff, WORKER_MARKDOWN_DIFF_LIMITS } from "./markdownDiff";
import {
  heightModel,
  MAX_SAMPLES_PER_KIND,
  rangeHeight,
  sectionHeight,
  sectionShape,
  UNCALIBRATED,
  wantsSample,
  withSample,
  type HeightCalibration,
} from "./sectionHeightEstimate";

function sectionsOf(before: string, after = before) {
  const result = computeMarkdownDiff(before, after, WORKER_MARKDOWN_DIFF_LIMITS);
  if (result.status !== "ready") throw new Error(result.message);
  return result.sections;
}

const TYPOGRAPHY = { proseLine: 20, proseCharacter: 10, proseInset: 0, codeLine: 16 };
const WIDTH = 400;
const height = (calibration: HeightCalibration, markdown: string, width = WIDTH) =>
  sectionHeight(sectionShape(sectionsOf(markdown)[0]!), heightModel(calibration), width);

describe("sectionShape", () => {
  it("tells prose, headings, code, tables and other blocks apart", () => {
    const [heading, paragraph, list, code, table, rule] = sectionsOf(
      [
        "# Title",
        "A short paragraph.",
        "- one\n- two\n- three",
        "```\na\nb\nc\n```",
        "| a | b |\n| - | - |\n| 1 | 2 |\n| 3 | 4 |",
        "---",
      ].join("\n\n"),
    ).map(sectionShape);
    expect(heading!.kind).toBe("heading");
    expect(paragraph).toEqual({ kind: "prose", changed: false, runs: [18], rows: 0 });
    expect(list!.kind).toBe("list");
    expect(list!.runs).toHaveLength(3);
    expect(code).toMatchObject({ kind: "code", rows: 3 });
    expect(table).toMatchObject({ kind: "table", rows: 3 });
    expect(rule!.kind).toBe("other");
  });

  it("marks added and removed sections, and reads each section once", () => {
    const sections = sectionsOf("Old text.", "New text.");
    expect(sections.map((section) => sectionShape(section).changed)).toEqual([true, true]);
    expect(sectionShape(sections[0]!)).toBe(sectionShape(sections[0]!));
  });
});

describe("sectionHeight", () => {
  it("is finite and positive for every kind before any sample, at any width", () => {
    const model = heightModel(UNCALIBRATED);
    const shapes = sectionsOf("# T\n\ntext\n\n```\nx\n```\n\n| a |\n| - |\n| 1 |\n\n---").map(
      sectionShape,
    );
    for (const shape of shapes) {
      for (const width of [0, 1, 400, 10_000]) {
        const estimate = sectionHeight(shape, model, width);
        expect(Number.isFinite(estimate) && estimate > 0).toBe(true);
      }
    }
  });

  it("wraps prose to the width: narrower is taller, by whole lines", () => {
    // 40 columns at 400 px: 100 characters wrap to 3 lines, and to 5 at 200 px.
    const [paragraph] = sectionsOf("x".repeat(100)).map(sectionShape);
    const calibration = withSample(UNCALIBRATED, paragraph!, 3 * 20 + 16, WIDTH, {
      typography: TYPOGRAPHY,
    });
    expect(height(calibration, "x".repeat(100))).toBe(76);
    expect(height(calibration, "x".repeat(100), 200)).toBe(5 * 20 + 16);
  });

  it("learns how full a wrapped paragraph's lines are", () => {
    const long = "word ".repeat(100).trim();
    const [paragraph] = sectionsOf(long).map(sectionShape);
    // 499 characters on 400 px lines took 10.5 lines' worth: about 8.4 px per character.
    const calibration = withSample(UNCALIBRATED, paragraph!, 11 * 20 + 16, WIDTH, {
      typography: TYPOGRAPHY,
      paragraph: { width: 400, lines: 11, characters: 499 },
    });
    expect(heightModel(calibration).proseCharacter).toBeCloseTo((400 * 10.5) / 499);
    // Its lines at the learned fill, plus the default margin.
    expect(height(calibration, long)).toBe(11 * 20 + 16);
    // A paragraph of a line or two says little about it.
    const short = withSample(UNCALIBRATED, paragraph!, 36, WIDTH, {
      typography: TYPOGRAPHY,
      paragraph: { width: 400, lines: 2, characters: 499 },
    });
    expect(heightModel(short).proseCharacter).toBe(10);
  });

  it("learns each kind's overhead from its own samples", () => {
    const [paragraph, code] = sectionsOf("Short.\n\n```\na\nb\n```").map(sectionShape);
    let calibration = withSample(UNCALIBRATED, paragraph!, 20 + 12, WIDTH, {
      typography: TYPOGRAPHY,
    });
    calibration = withSample(calibration, code!, 2 * 16 + 30, WIDTH);
    const model = heightModel(calibration);
    expect(sectionHeight(paragraph!, model, WIDTH)).toBe(32);
    expect(sectionHeight(code!, model, WIDTH)).toBe(62);
  });

  it("keeps changed sections' padding out of unchanged ones' estimate", () => {
    const [removed, added, unchanged] = sectionsOf("Old.\n\nSame.", "New.\n\nSame.").map(
      sectionShape,
    );
    let calibration = withSample(UNCALIBRATED, removed!, 28, WIDTH, { typography: TYPOGRAPHY });
    calibration = withSample(calibration, unchanged!, 36, WIDTH);
    const model = heightModel(calibration);
    expect(sectionHeight(unchanged!, model, WIDTH)).toBe(36);
    expect(sectionHeight(added!, model, WIDTH)).toBe(28);
    // A changed section with no samples of its own is estimated like an unchanged one.
    const unchangedOnly = heightModel(withSample(UNCALIBRATED, unchanged!, 36, WIDTH));
    expect(sectionHeight(added!, unchangedOnly, WIDTH)).toBe(36);
  });

  it("learns only the fill from a long paragraph, not short paragraphs' margins", () => {
    const long = "word ".repeat(100).trim();
    const [tall, short] = sectionsOf(`${long}\n\nShort.`).map(sectionShape);
    // Measured a line taller than the estimate counts: rounding in the wrap, not a margin.
    const calibration = withSample(UNCALIBRATED, tall!, 12 * 20 + 16, WIDTH, {
      typography: TYPOGRAPHY,
      paragraph: { width: 400, lines: 12, characters: 499 },
    });
    expect(calibration.wrapped.count).toBe(1);
    expect(calibration.samples).toEqual({});
    expect(sectionHeight(short!, heightModel(calibration), WIDTH)).toBe(20 + 16);
  });

  it("keeps lists' margins out of paragraphs' estimate", () => {
    const [paragraph, list] = sectionsOf("Text.\n\n- one\n- two").map(sectionShape);
    let calibration = withSample(UNCALIBRATED, paragraph!, 36, WIDTH, { typography: TYPOGRAPHY });
    calibration = withSample(calibration, list!, 2 * 20 + 24, WIDTH);
    const model = heightModel(calibration);
    expect(sectionHeight(paragraph!, model, WIDTH)).toBe(36);
    expect(sectionHeight(list!, model, WIDTH)).toBe(64);
  });

  it("never lets a table calibrate prose", () => {
    const sections = sectionsOf(
      "| a | b |\n| - | - |\n" + "| 1 | 2 |\n".repeat(99) + "\nA paragraph of prose.",
    );
    const [table, paragraph] = sections.map(sectionShape);
    // A tall table first, as at the top of a document.
    const calibration = withSample(UNCALIBRATED, table!, 100 * 30, WIDTH);
    const model = heightModel(calibration);
    expect(sectionHeight(table!, model, WIDTH)).toBe(3000);
    expect(sectionHeight(paragraph!, model, WIDTH)).toBe(
      sectionHeight(paragraph!, heightModel(UNCALIBRATED), WIDTH),
    );
  });
});

describe("withSample", () => {
  it("ignores heights that aren't real measurements", () => {
    const [paragraph] = sectionsOf("Text.").map(sectionShape);
    for (const measured of [0, -5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(withSample(UNCALIBRATED, paragraph!, measured, WIDTH)).toBe(UNCALIBRATED);
    }
  });

  it("keeps the typography it read first, and ignores values it couldn't read", () => {
    const [paragraph, other] = sectionsOf("Text.\n\nMore.").map(sectionShape);
    let calibration = withSample(UNCALIBRATED, paragraph!, 40, WIDTH, {
      typography: { proseLine: 20, proseCharacter: Number.NaN, proseInset: 0 },
    });
    calibration = withSample(calibration, other!, 40, WIDTH, {
      typography: { proseLine: 30, proseCharacter: 9, proseInset: -3 },
    });
    expect(calibration.typography).toEqual({ proseLine: 20, proseCharacter: 9, proseInset: 0 });
  });

  it("stops taking samples of a kind once it has enough, but still learns from long paragraphs", () => {
    const [paragraph] = sectionsOf("Text.").map(sectionShape);
    let calibration: HeightCalibration = UNCALIBRATED;
    for (let index = 0; index < MAX_SAMPLES_PER_KIND; index += 1) {
      calibration = withSample(calibration, paragraph!, 40, WIDTH, { typography: TYPOGRAPHY });
    }
    expect(wantsSample(calibration, paragraph!)).toBe(false);
    expect(withSample(calibration, paragraph!, 99, WIDTH)).toBe(calibration);
    const [long] = sectionsOf("word ".repeat(100).trim()).map(sectionShape);
    expect(wantsSample(calibration, long!)).toBe(true);
    const taught = withSample(calibration, long!, 236, WIDTH, {
      paragraph: { width: 400, lines: 11, characters: 499 },
    });
    expect(taught.wrapped.count).toBe(1);
    expect(taught.samples).toBe(calibration.samples);
  });
});

describe("rangeHeight", () => {
  it("sums its sections, so a group of many short paragraphs isn't a fixed guess per section", () => {
    const sections = sectionsOf(
      Array.from({ length: 10 }, (_, index) => `Paragraph ${index}.`).join("\n\n"),
    );
    const calibration = withSample(UNCALIBRATED, sectionShape(sections[0]!), 36, WIDTH, {
      typography: TYPOGRAPHY,
    });
    const model = heightModel(calibration);
    expect(rangeHeight(sections, 0, 10, model, WIDTH)).toBe(360);
    expect(rangeHeight(sections, 2, 5, model, WIDTH)).toBe(108);
  });
});
