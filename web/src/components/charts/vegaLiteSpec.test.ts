import { describe, expect, it } from "vitest";
import {
  MAX_VEGA_LITE_SEQUENCE_ROWS,
  MAX_VEGA_LITE_SPEC_CHARS,
  isVegaLiteLanguage,
  isVegaLitePath,
  parseVegaLiteSpec,
} from "./vegaLiteSpec";

const INLINE_SPEC = {
  $schema: "https://vega.github.io/schema/vega-lite/v6.json",
  data: { values: [{ a: 1 }] },
  mark: "point",
  encoding: { x: { field: "a", type: "quantitative" } },
};

function errorOf(source: string): string {
  const result = parseVegaLiteSpec(source);
  if (result.ok) throw new Error("expected the spec to be refused");
  return result.error;
}

describe("parseVegaLiteSpec", () => {
  it("accepts an inline-data spec", () => {
    expect(parseVegaLiteSpec(JSON.stringify(INLINE_SPEC))).toEqual({ ok: true, spec: INLINE_SPEC });
  });

  it("refuses specs over the size cap before parsing them", () => {
    // Invalid JSON past the cap reports the size, so the text was never parsed.
    expect(errorOf(`{${"x".repeat(MAX_VEGA_LITE_SPEC_CHARS)}`)).toMatch(/too large/);
  });

  it("accepts rows with a url column", () => {
    const rows = [{ url: "https://github.com/org/repo/pull/1", hits: 5 }];

    expect(parseVegaLiteSpec(JSON.stringify({ data: { values: rows }, mark: "bar" })).ok).toBe(
      true,
    );
  });

  it("walks arrays too long to spread onto the call stack", () => {
    const domain = new Array<number>(150_000).fill(0);
    const spec = { ...INLINE_SPEC, encoding: { x: { field: "a", scale: { domain } } } };

    expect(parseVegaLiteSpec(JSON.stringify(spec)).ok).toBe(true);
  });

  it.each([
    ["invalid JSON", "{mark: bar}", /valid JSON/],
    ["an array", "[1, 2]", /JSON object/],
    ["a scalar", "42", /JSON object/],
    [
      "a full Vega spec",
      JSON.stringify({ $schema: "https://vega.github.io/schema/vega/v6.json", marks: [] }),
      /Only Vega-Lite/,
    ],
  ])("refuses %s", (_label, source, message) => {
    expect(errorOf(source)).toMatch(message);
  });

  it.each([
    ["top-level data", { ...INLINE_SPEC, data: { url: "https://example.com/data.csv" } }],
    [
      "a layer's data",
      { layer: [{ data: { url: "data/cars.json" }, mark: "point" }], data: { values: [] } },
    ],
    [
      "a lookup source",
      {
        ...INLINE_SPEC,
        transform: [
          { lookup: "a", from: { data: { url: "https://example.com/x.json" }, key: "a" } },
        ],
      },
    ],
  ])("refuses a URL in %s", (_label, spec) => {
    expect(errorOf(JSON.stringify(spec))).toMatch(/inline data/);
  });

  it("allows a bounded sequence and refuses an unbounded one", () => {
    const sequence = (stop: number, step?: number) =>
      JSON.stringify({ data: { sequence: { start: 0, stop, step, as: "x" } }, mark: "line" });

    expect(parseVegaLiteSpec(sequence(MAX_VEGA_LITE_SEQUENCE_ROWS)).ok).toBe(true);
    expect(errorOf(sequence(MAX_VEGA_LITE_SEQUENCE_ROWS + 1))).toMatch(/sequence/);
    expect(errorOf(sequence(10, 0))).toMatch(/sequence/);
  });

  it("refuses an unbounded impute range and sequence() in expressions", () => {
    const impute = { impute: "y", key: "x", keyvals: { start: 0, stop: 1e10 } };
    const calculate = { calculate: "length(sequence(0, 1e9))", as: "n" };
    const axisExpression = (labelExpr: string) => ({
      ...INLINE_SPEC,
      encoding: { x: { field: "a", type: "quantitative", axis: { labelExpr } } },
    });

    expect(errorOf(JSON.stringify({ ...INLINE_SPEC, transform: [impute] }))).toMatch(/impute/);
    for (const spec of [
      { ...INLINE_SPEC, transform: [calculate] },
      axisExpression("length(sequence(0, 1000000000))"),
      axisExpression("length(sequence (0, 1000000000))"),
      { ...INLINE_SPEC, params: [{ name: "n", expr: "length(sequence(0, 1e9))" }] },
    ]) {
      expect(errorOf(JSON.stringify(spec))).toMatch(/sequence\(\)/);
    }
    // Prose that mentions a sequence is fine.
    const prose = {
      ...INLINE_SPEC,
      title: "Reads per sequence(bp)",
      encoding: { x: { field: "sequence(bp)", type: "quantitative" } },
    };
    expect(parseVegaLiteSpec(JSON.stringify(prose)).ok).toBe(true);
  });

  it("refuses a number format wider than the limit and allows ordinary ones", () => {
    const withAxisFormat = (format: string) => ({
      ...INLINE_SPEC,
      encoding: { x: { field: "a", type: "quantitative", axis: { format } } },
    });

    expect(errorOf(JSON.stringify(withAxisFormat("01001d")))).toMatch(/number format/);
    expect(errorOf(JSON.stringify({ ...INLINE_SPEC, config: { numberFormat: "02000d" } }))).toMatch(
      /number format/,
    );
    for (const format of ["01000d", "$,.2f", "%Y-%m-%d", "%b 2024", "FY2025 Q%q"]) {
      expect(parseVegaLiteSpec(JSON.stringify(withAxisFormat(format))).ok).toBe(true);
    }
  });

  it.each([
    ["too many bins", { bin: { maxbins: 5_000 } }, /maxbins/],
    ["too many density steps", { density: "a", steps: 1e7 }, /steps/],
    ["too fine a quantile step", { quantile: "a", step: 1e-8 }, /quantile/],
    [
      "too high a regression order",
      { regression: "a", on: "a", method: "poly", order: 100 },
      /order/,
    ],
  ])("refuses %s", (_label, transform, message) => {
    expect(errorOf(JSON.stringify({ ...INLINE_SPEC, transform: [transform] }))).toMatch(message);
  });

  it("refuses custom format types, which call expression functions by name", () => {
    for (const customFormatTypes of [true, 1]) {
      const spec = { ...INLINE_SPEC, config: { customFormatTypes } };
      expect(errorOf(JSON.stringify(spec))).toMatch(/Custom format types/);
    }
  });
});

describe("isVegaLiteLanguage", () => {
  it.each([
    ["vega-lite", true],
    ["Vega-Lite", true],
    ["vegalite", true],
    ["vega", false],
    ["json", false],
  ])("%s → %s", (language, expected) => {
    expect(isVegaLiteLanguage(language)).toBe(expected);
  });
});

describe("isVegaLitePath", () => {
  it.each([
    ["charts/sales.vl.json", true],
    ["Sales.VEGA-LITE.json", true],
    ["sales.json", false],
    ["sales.vl.json.bak", false],
  ])("%s → %s", (path, expected) => {
    expect(isVegaLitePath(path)).toBe(expected);
  });
});
