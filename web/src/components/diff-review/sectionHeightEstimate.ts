// The height a rich diff section is expected to take before the browser lays it out. A group of
// sections that has never been on screen reserves the sum, so jumping past it lands where the
// content really is. Each kind of block has its own model, calibrated only by laid-out sections
// of the same kind, so a table never sets the height of prose.

import type { Element, RootContent } from "hast";
import type { MarkdownDiffSection } from "./markdownDiff";

export type SectionShapeKind = "prose" | "list" | "heading" | "code" | "table" | "other";

/** What a section's height depends on, read once from its block. */
export interface SectionShape {
  kind: SectionShapeKind;
  /** Added and removed sections are padded and trimmed differently from unchanged ones. */
  changed: boolean;
  /** Prose and lists: the characters of each run of text that wraps on its own, as an item. */
  runs: readonly number[];
  /** Code: its lines. Table: its rows. */
  rows: number;
}

/** How the preview sets text, read from a laid-out section. */
export interface Typography {
  /** One line of prose. */
  proseLine: number;
  /** A prose character's share of a wrapped line, before any wrapped paragraph has shown it. */
  proseCharacter: number;
  /** How much narrower prose text is than the sections' container. */
  proseInset: number;
  /** One line of code. */
  codeLine: number;
}

/** A laid-out section's measured height, at the sections' width then. */
export interface HeightSample {
  shape: SectionShape;
  height: number;
  width: number;
}

/** What laid-out sections have shown so far. */
export interface HeightCalibration {
  /** What's been read so far; the rest is still the default. */
  typography: Partial<Typography>;
  /** Wrapped paragraphs' widths times their lines, and their characters: a line's real fill. */
  wrapped: { width: number; characters: number; count: number };
  /** Samples by kind, and by whether the section changed. */
  samples: Readonly<Record<string, readonly HeightSample[]>>;
}

/** Samples per kind beyond which more change nothing worth a re-render. */
export const MAX_SAMPLES_PER_KIND = 12;

/** Lines a paragraph needs before its fill says how many characters a line holds. */
const WRAPPED_LINES = 3;

/** Characters a paragraph needs before it's worth reading for how full its lines are. */
const WRAPPED_CHARACTERS = 200;

export const UNCALIBRATED: HeightCalibration = {
  typography: {},
  wrapped: { width: 0, characters: 0, count: 0 },
  samples: {},
};

/** Typical values at the preview's default type size, until a section shows the real ones. */
const DEFAULT_TYPOGRAPHY: Typography = {
  proseLine: 24,
  proseCharacter: 9.5,
  proseInset: 27,
  codeLine: 17,
};

/**
 * A prose character's share of a wrapped line, per pixel of font size: a glyph's advance plus
 * what word wrapping leaves at a line's end.
 */
export const PROSE_CHARACTER_PER_FONT_PIXEL = 0.68;

/** Before any sample, a kind's height besides its lines or rows: margins, padding, borders. */
const DEFAULT_OVERHEAD: Record<SectionShapeKind, number> = {
  prose: 16,
  list: 24,
  heading: 48,
  code: 45,
  table: 0,
  other: 48,
};

/** Before any sample, a table row's height. */
const DEFAULT_ROW = 36;

const shapes = new WeakMap<MarkdownDiffSection, SectionShape>();

function elementChildren(node: { children?: readonly RootContent[] }): Element[] {
  return (node.children ?? []).filter((child): child is Element => child.type === "element");
}

/** What `section`'s height depends on; read once per section, from its block and text. */
export function sectionShape(section: MarkdownDiffSection): SectionShape {
  const cached = shapes.get(section);
  if (cached) return cached;
  const shape = { ...readShape(section), changed: section.kind !== "unchanged" };
  shapes.set(section, shape);
  return shape;
}

function readShape(section: MarkdownDiffSection): Omit<SectionShape, "changed"> {
  const block = section.block;
  const other = { kind: "other", runs: [], rows: 0 } as const;
  if (section.oversized || block.type !== "element") return other;
  const tag = block.tagName;
  const text = section.text.length;
  if (/^h[1-6]$/.test(tag)) return { kind: "heading", runs: [text], rows: 0 };
  if (tag === "pre") {
    const codeLines = section.text.replace(/\n+$/, "").split("\n").length;
    return { kind: "code", runs: [], rows: codeLines };
  }
  if (tag === "table") {
    const rows = elementChildren(block).reduce(
      (count, part) => count + elementChildren(part).filter((row) => row.tagName === "tr").length,
      0,
    );
    return { kind: "table", runs: [], rows: Math.max(rows, 1) };
  }
  if (tag === "ul" || tag === "ol") {
    const items = elementChildren(block).filter((item) => item.tagName === "li");
    const each = text / Math.max(items.length, 1);
    return { kind: "list", runs: items.length ? items.map(() => each) : [text], rows: 0 };
  }
  if (tag === "p" || tag === "blockquote") {
    // A paragraph that holds an image is as tall as the image, not its text.
    if (elementChildren(block).some((child) => child.tagName === "img")) return other;
    return { kind: "prose", runs: [text], rows: 0 };
  }
  return other;
}

/** The estimate's working values, derived once per calibration. */
export interface HeightModel {
  typography: Typography;
  /** A prose character's share of a line, in pixels. */
  proseCharacter: number;
  overhead: (shape: SectionShape) => number;
  tableRow: (shape: SectionShape) => number;
}

type LineModel = Pick<HeightModel, "typography" | "proseCharacter">;

/** Prose characters that fit on one line at the sections' `width`. */
function columns(model: LineModel, width: number): number {
  const textWidth = Math.max(width - model.typography.proseInset, model.proseCharacter);
  return Math.max(1, Math.floor(textWidth / model.proseCharacter));
}

/** The text or code lines `shape` has at the sections' `width`; 0 for the other kinds. */
function lines(shape: SectionShape, model: LineModel, width: number): number {
  if (shape.kind === "prose" || shape.kind === "list") {
    const perLine = columns(model, width);
    return shape.runs.reduce((count, run) => count + Math.max(1, Math.ceil(run / perLine)), 0);
  }
  return shape.kind === "code" ? shape.rows : 0;
}

function lineHeight(shape: SectionShape, typography: Typography): number {
  if (shape.kind === "prose" || shape.kind === "list") return typography.proseLine;
  return shape.kind === "code" ? typography.codeLine : 0;
}

function sampleKey(kind: SectionShapeKind, changed: boolean): string {
  return changed ? `${kind}:changed` : kind;
}

function mean(values: readonly number[]): number | undefined {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : undefined;
}

/** The working values `calibration` gives. */
export function heightModel(calibration: HeightCalibration): HeightModel {
  const typography = { ...DEFAULT_TYPOGRAPHY, ...calibration.typography };
  const { wrapped } = calibration;
  const proseCharacter =
    wrapped.characters > 0 ? wrapped.width / wrapped.characters : typography.proseCharacter;
  const base = { typography, proseCharacter };
  const samplesOf = (key: string) => calibration.samples[key] ?? [];
  // A kind's height besides its lines: what its samples had on average past their lines.
  const overheads = new Map<string, number | undefined>();
  const learnedOverhead = (key: string) => {
    if (!overheads.has(key)) {
      const residuals = samplesOf(key).map(
        (sample) =>
          sample.height -
          lines(sample.shape, base, sample.width) * lineHeight(sample.shape, typography),
      );
      overheads.set(key, mean(residuals));
    }
    return overheads.get(key);
  };
  const learnedRow = (key: string) => {
    const samples = samplesOf(key);
    const rows = samples.reduce((sum, sample) => sum + sample.shape.rows, 0);
    return rows > 0 ? samples.reduce((sum, sample) => sum + sample.height, 0) / rows : undefined;
  };
  const tableRows = new Map<boolean, number>();
  const overheadByKind = new Map<string, number>();
  return {
    typography,
    proseCharacter,
    // A changed section without samples of its own is closest to an unchanged one of its kind.
    overhead: (shape) => {
      const key = sampleKey(shape.kind, shape.changed);
      let value = overheadByKind.get(key);
      if (value === undefined) {
        value = Math.max(
          0,
          learnedOverhead(key) ??
            learnedOverhead(sampleKey(shape.kind, false)) ??
            DEFAULT_OVERHEAD[shape.kind],
        );
        overheadByKind.set(key, value);
      }
      return value;
    },
    // A table's header and borders are spread across its rows.
    tableRow: (shape) => {
      let row = tableRows.get(shape.changed);
      if (row === undefined) {
        row =
          learnedRow(sampleKey("table", shape.changed)) ??
          learnedRow(sampleKey("table", false)) ??
          DEFAULT_ROW;
        tableRows.set(shape.changed, row);
      }
      return row;
    },
  };
}

/** `shape`'s expected height in pixels at the sections' `width`: always finite and positive. */
export function sectionHeight(shape: SectionShape, model: HeightModel, width: number): number {
  const height =
    shape.kind === "table"
      ? shape.rows * model.tableRow(shape)
      : model.overhead(shape) + lines(shape, model, width) * lineHeight(shape, model.typography);
  return Number.isFinite(height) && height >= 1 ? height : 1;
}

/** The sections `start` up to `end`, summed and rounded. */
export function rangeHeight(
  sections: readonly MarkdownDiffSection[],
  start: number,
  end: number,
  model: HeightModel,
  width: number,
): number {
  let height = 0;
  for (let index = start; index < end; index += 1) {
    height += sectionHeight(sectionShape(sections[index]!), model, width);
  }
  return Math.round(height);
}

function wantsHeight(calibration: HeightCalibration, shape: SectionShape): boolean {
  // A long run's height is mostly its wrapped lines, which the estimate can be a line or two off
  // on: what's left over says little about a short paragraph's margins. It teaches the fill.
  const wraps = shape.kind === "prose" || shape.kind === "list";
  if (wraps && shape.runs.some((run) => run >= WRAPPED_CHARACTERS)) return false;
  const samples = calibration.samples[sampleKey(shape.kind, shape.changed)];
  return (samples?.length ?? 0) < MAX_SAMPLES_PER_KIND;
}

function wantsWrap(calibration: HeightCalibration, shape: SectionShape): boolean {
  return (
    calibration.wrapped.count < MAX_SAMPLES_PER_KIND &&
    shape.kind === "prose" &&
    shape.runs.length === 1 &&
    shape.runs[0]! >= WRAPPED_CHARACTERS
  );
}

/** Whether another laid-out section like `shape` would still teach the estimate anything. */
export function wantsSample(calibration: HeightCalibration, shape: SectionShape): boolean {
  return wantsHeight(calibration, shape) || wantsWrap(calibration, shape);
}

/** What the page showed beside a laid-out section, for the calibration to keep. */
export interface SectionReading {
  /** Values read from its styles; one already known is kept. */
  typography?: Partial<Typography>;
  /** A paragraph's own text: its width, its height in lines, and its characters. */
  paragraph?: { width: number; lines: number; characters: number };
}

/** `calibration` once a laid-out section of `shape` measured `height` at the sections' `width`. */
export function withSample(
  calibration: HeightCalibration,
  shape: SectionShape,
  height: number,
  width: number,
  reading: SectionReading = {},
): HeightCalibration {
  if (!(height > 0) || !Number.isFinite(height)) return calibration;
  // Sizes must be positive; prose may have no inset at all.
  const known = Object.fromEntries(
    Object.entries(reading.typography ?? {}).filter(
      ([name, value]) => Number.isFinite(value) && (name === "proseInset" ? value >= 0 : value > 0),
    ),
  );
  const paragraph = reading.paragraph;
  // Only a paragraph of a few lines shows how full a line is; its last line is half full.
  const wraps =
    wantsWrap(calibration, shape) &&
    paragraph !== undefined &&
    paragraph.lines >= WRAPPED_LINES &&
    paragraph.characters > 0 &&
    paragraph.width > 0;
  const measures = wantsHeight(calibration, shape);
  const teachesTypography = Object.keys(known).some((name) => !(name in calibration.typography));
  if (!wraps && !measures && !teachesTypography) return calibration;
  const key = sampleKey(shape.kind, shape.changed);
  const { wrapped } = calibration;
  return {
    typography: { ...known, ...calibration.typography },
    wrapped: wraps
      ? {
          width: wrapped.width + paragraph.width * (paragraph.lines - 0.5),
          characters: wrapped.characters + paragraph.characters,
          count: wrapped.count + 1,
        }
      : wrapped,
    samples: measures
      ? {
          ...calibration.samples,
          [key]: [...(calibration.samples[key] ?? []), { shape, height, width }],
        }
      : calibration.samples,
  };
}
