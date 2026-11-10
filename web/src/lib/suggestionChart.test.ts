import { describe, expect, it } from "vitest";
import { chartRows, formatChartValue, parseSuggestionChart } from "./suggestionChart";

const valid = {
  version: 1,
  type: "line",
  title: "p95 latency",
  unit: "ms",
  x: ["Mon", "Tue", "Wed"],
  series: [
    { name: "p95", values: [10, null, 12], emphasis: true },
    { name: "x", values: [1, 2, 3] },
  ],
  highlight: { from: "Tue", to: "Wed" },
  markers: [{ x: "Tue", label: "PR merged" }],
  source: { label: "Dashboard", href: "https://example.com/d" },
};

describe("parseSuggestionChart", () => {
  it("accepts a version 1 chart", () => {
    const chart = parseSuggestionChart(valid);
    expect(chart).toMatchObject({
      type: "line",
      unit: "ms",
      highlight: { from: "Tue", to: "Wed" },
      markers: [{ x: "Tue", label: "PR merged" }],
      source: { label: "Dashboard", href: "https://example.com/d" },
    });
    expect(chart?.series.map((s) => s.emphasis)).toEqual([true, false]);
  });

  it("keeps the source but drops a non-web link", () => {
    const chart = parseSuggestionChart({
      ...valid,
      source: { label: "Notes", href: "javascript:alert(1)" },
    });
    expect(chart?.source).toEqual({ label: "Notes", href: null });
  });

  it.each([
    ["no chart", null],
    ["unknown version", { ...valid, version: 2 }],
    ["unknown type", { ...valid, type: "pie" }],
    ["no x labels", { ...valid, x: [] }],
    ["too many series", { ...valid, series: Array(5).fill(valid.series[1]) }],
    ["series length mismatch", { ...valid, series: [{ name: "a", values: [1] }] }],
    ["non-numeric value", { ...valid, series: [{ name: "a", values: [1, "2", 3] }] }],
    ["infinite value", { ...valid, series: [{ name: "a", values: [1, Infinity, 3] }] }],
    ["highlight off the axis", { ...valid, highlight: { from: "Sun", to: "Tue" } }],
    ["marker off the axis", { ...valid, markers: [{ x: "Sun", label: "x" }] }],
    ["too many markers", { ...valid, markers: Array(4).fill(valid.markers[0]) }],
    ["missing title", { ...valid, title: "" }],
    ["array", [valid]],
  ])("skips a chart with %s", (_name, raw) => {
    expect(parseSuggestionChart(raw)).toBeNull();
  });
});

it("builds one row per x label with collision-free series keys", () => {
  const chart = parseSuggestionChart({ ...valid, series: [{ name: "x", values: [1, 2, 3] }] })!;
  expect(chartRows(chart)).toEqual([
    { x: "Mon", s0: 1 },
    { x: "Tue", s0: 2 },
    { x: "Wed", s0: 3 },
  ]);
});

it("formats values with the unit", () => {
  expect(formatChartValue(12, "ms")).toBe("12 ms");
  expect(formatChartValue(0.125, "%")).toBe("0.13%");
  expect(formatChartValue(3.5, null)).toBe("3.5");
});
