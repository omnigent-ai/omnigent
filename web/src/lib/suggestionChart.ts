// Validates the declarative chart an agent can attach to an Inbox suggestion
// (version 1, mirroring the server's schema). Anything this build cannot draw
// returns `null`, and the card shows the body alone.
import { safeWebHref } from "@/lib/proactiveInbox";

export interface SuggestionChartSeries {
  name: string;
  values: (number | null)[];
  emphasis: boolean;
}

export interface SuggestionChart {
  type: "line" | "bar";
  title: string;
  unit: string | null;
  x: string[];
  series: SuggestionChartSeries[];
  highlight: { from: string; to: string } | null;
  markers: { x: string; label: string }[];
  source: { label: string; href: string | null } | null;
}

const MAX_SERIES = 4;
const MAX_X_LABELS = 60;
const MAX_MARKERS = 3;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

function parseSeries(value: unknown, length: number): SuggestionChartSeries | null {
  if (!isRecord(value) || !nonEmptyString(value.name) || !Array.isArray(value.values)) return null;
  if (value.values.length !== length) return null;
  const values = value.values.map((v) =>
    v === null ? null : typeof v === "number" && Number.isFinite(v) ? v : undefined,
  );
  if (values.includes(undefined)) return null;
  return {
    name: value.name,
    values: values as (number | null)[],
    emphasis: value.emphasis === true,
  };
}

/** The chart if this build can draw it, else `null`. */
export function parseSuggestionChart(raw: unknown): SuggestionChart | null {
  if (!isRecord(raw) || raw.version !== 1) return null;
  if (raw.type !== "line" && raw.type !== "bar") return null;
  if (!nonEmptyString(raw.title)) return null;
  const x = raw.x;
  if (!Array.isArray(x) || x.length === 0 || x.length > MAX_X_LABELS) return null;
  if (!x.every((label) => typeof label === "string")) return null;
  const labels = new Set(x as string[]);
  if (!Array.isArray(raw.series) || raw.series.length === 0 || raw.series.length > MAX_SERIES) {
    return null;
  }
  const series = raw.series.map((entry) => parseSeries(entry, x.length));
  if (series.some((entry) => entry === null)) return null;

  let highlight: SuggestionChart["highlight"] = null;
  if (raw.highlight !== undefined && raw.highlight !== null) {
    const h = raw.highlight;
    if (!isRecord(h) || typeof h.from !== "string" || typeof h.to !== "string") return null;
    if (!labels.has(h.from) || !labels.has(h.to)) return null;
    highlight = { from: h.from, to: h.to };
  }

  const rawMarkers = raw.markers ?? [];
  if (!Array.isArray(rawMarkers) || rawMarkers.length > MAX_MARKERS) return null;
  const markers: SuggestionChart["markers"] = [];
  for (const marker of rawMarkers) {
    if (!isRecord(marker) || typeof marker.x !== "string" || !nonEmptyString(marker.label)) {
      return null;
    }
    if (!labels.has(marker.x)) return null;
    markers.push({ x: marker.x, label: marker.label });
  }

  let source: SuggestionChart["source"] = null;
  if (raw.source !== undefined && raw.source !== null) {
    if (!isRecord(raw.source) || !nonEmptyString(raw.source.label)) return null;
    const href = typeof raw.source.href === "string" ? safeWebHref(raw.source.href) : null;
    source = { label: raw.source.label, href };
  }

  if (raw.unit !== undefined && raw.unit !== null && typeof raw.unit !== "string") return null;
  return {
    type: raw.type,
    title: raw.title,
    unit: typeof raw.unit === "string" && raw.unit !== "" ? raw.unit : null,
    x: x as string[],
    series: series as SuggestionChartSeries[],
    highlight,
    markers,
    source,
  };
}

/** Rows recharts reads: one per x label, with a key per series. */
export function chartRows(chart: SuggestionChart): Record<string, string | number | null>[] {
  return chart.x.map((label, index) => {
    const row: Record<string, string | number | null> = { x: label };
    chart.series.forEach((series, seriesIndex) => {
      row[seriesKey(seriesIndex)] = series.values[index] ?? null;
    });
    return row;
  });
}

/** A data key that cannot collide with `x` whatever the series is named. */
export function seriesKey(index: number): string {
  return `s${index}`;
}

/** A value with the chart's unit, e.g. `"12 ms"` or `"40%"`. */
export function formatChartValue(value: number, unit: string | null): string {
  const text = Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, "");
  if (!unit) return text;
  return unit === "%" ? `${text}%` : `${text} ${unit}`;
}
