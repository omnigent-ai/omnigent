// Parsing and safety checks for Vega-Lite chart specs written by agents. The
// renderer itself loads lazily; this module stays small enough for the main bundle.

/** Largest spec, in characters, that renders as a chart; bigger ones show their source. */
export const MAX_VEGA_LITE_SPEC_CHARS = 1_000_000;

/** Most rows a `sequence` generator or impute `keyvals` range may produce. */
export const MAX_VEGA_LITE_SEQUENCE_ROWS = 100_000;

/** Code fence languages that render as a chart. */
const VEGA_LITE_LANGUAGES = new Set(["vega-lite", "vegalite"]);

/** File suffixes the file viewer previews as a chart. */
const VEGA_LITE_SUFFIXES = [".vl.json", ".vega-lite.json"];

/** A parsed Vega-Lite spec. */
export type VegaLiteSpec = Record<string, unknown>;

export type VegaLiteSpecResult = { ok: true; spec: VegaLiteSpec } | { ok: false; error: string };

/** Whether a code fence's language (e.g. ``"vega-lite"``) is a Vega-Lite chart. */
export function isVegaLiteLanguage(language: string): boolean {
  return VEGA_LITE_LANGUAGES.has(language.trim().toLowerCase());
}

/** Whether a workspace path (e.g. ``"charts/sales.vl.json"``) holds a Vega-Lite spec. */
export function isVegaLitePath(path: string): boolean {
  const lower = path.toLowerCase();
  return VEGA_LITE_SUFFIXES.some((suffix) => lower.endsWith(suffix));
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Keys holding inline rows, which may contain any text, such as a `url` column. */
const INLINE_DATA_KEYS = new Set(["values", "datasets"]);

/** Keys whose string values are Vega expressions, besides `expr` and `*Expr` (e.g. `labelExpr`). */
const EXPRESSION_KEYS = new Set(["calculate", "filter", "test", "signal", "update", "init"]);

function isExpressionKey(key: string): boolean {
  return EXPRESSION_KEYS.has(key) || key === "expr" || key.endsWith("Expr");
}

// An early, readable refusal; the renderer caps sequence() however a call is spelled.
const SEQUENCE_CALL = /\bsequence\s*\(/;

/** Widest number a format specifier may ask for, e.g. ``"01000d"`` (zero-padded to 1,000 digits). */
export const MAX_VEGA_LITE_FORMAT_WIDTH = 1_000;

// d3-format's specifier: [[fill]align][sign][symbol][0][width][,][.precision][~][type].
const NUMBER_FORMAT = /^(?:.?[<>=^])?[+\-( ]?[$#]?0?(\d+)?,?(?:\.\d+)?~?[a-z%]?$/i;

/**
 * Whether a number format specifier asks for a field wider than
 * {@link MAX_VEGA_LITE_FORMAT_WIDTH}. Time formats such as ``"%b 2024"`` aren't
 * number specifiers and never count.
 */
export function isOversizedFormat(specifier: string): boolean {
  const width = specifier.match(NUMBER_FORMAT)?.[1];
  return width !== undefined && Number(width) > MAX_VEGA_LITE_FORMAT_WIDTH;
}

/** Literal limits for transform parameters that size their output. */
const TRANSFORM_LIMITS: Record<string, number> = {
  maxbins: 1_000, // bin
  steps: 10_000, // density
  minsteps: 10_000,
  maxsteps: 10_000,
  order: 20, // regression
};

/** Rows a `sequence` generator or impute `keyvals` range produces; unknown bounds count as unbounded. */
function rangeRowCount(range: Record<string, unknown>): number {
  const { start = 0, stop, step = 1 } = range;
  if (typeof start !== "number" || typeof stop !== "number" || typeof step !== "number") {
    return Number.POSITIVE_INFINITY;
  }
  if (step === 0) return Number.POSITIVE_INFINITY;
  return Math.max(0, Math.ceil((stop - start) / step));
}

/** The first reason *spec* can't render safely, or null when it can. */
function unsafeSpecReason(spec: VegaLiteSpec): string | null {
  const pending: [key: string, value: unknown][] = [["", spec]];
  while (pending.length > 0) {
    const [key, node] = pending.pop()!;
    if (typeof node === "string") {
      if (isExpressionKey(key) && SEQUENCE_CALL.test(node)) {
        return "Expressions can't call sequence(). Use a `sequence` data generator instead.";
      }
      // Axis and legend formats run outside expressions, straight through the formatter.
      if ((key === "format" || key.endsWith("Format")) && isOversizedFormat(node)) {
        return `A number format can be at most ${MAX_VEGA_LITE_FORMAT_WIDTH.toLocaleString()} characters wide.`;
      }
      continue;
    }
    if (Array.isArray(node)) {
      for (const item of node) pending.push([key, item]);
      continue;
    }
    if (!isPlainObject(node)) continue;
    // Custom format types call any expression function by name, such as `sequence`.
    if (node.customFormatTypes) {
      return "Custom format types aren't supported in charts.";
    }
    for (const [name, limit] of Object.entries(TRANSFORM_LIMITS)) {
      const value = node[name];
      if (typeof value === "number" && value > limit) {
        return `\`${name}\` can be at most ${limit.toLocaleString()}.`;
      }
    }
    // A quantile transform computes 1 / step probabilities.
    if (typeof node.quantile === "string" && typeof node.step === "number" && node.step < 1e-4) {
      return "A quantile `step` must be at least 0.0001.";
    }
    // Data, lookups and image marks name remote files with a string `url`.
    if (typeof node.url === "string") {
      return "Charts can only use inline data. Put the rows in `data.values` instead of loading a URL.";
    }
    for (const range of [node.sequence, node.keyvals]) {
      if (isPlainObject(range) && rangeRowCount(range) > MAX_VEGA_LITE_SEQUENCE_ROWS) {
        return `A sequence or impute range can generate at most ${MAX_VEGA_LITE_SEQUENCE_ROWS.toLocaleString()} rows, with numeric start, stop and step.`;
      }
    }
    for (const [childKey, value] of Object.entries(node)) {
      if (!INLINE_DATA_KEYS.has(childKey)) pending.push([childKey, value]);
    }
  }
  return null;
}

/**
 * Parse *source* as a Vega-Lite spec that is safe to render.
 *
 * Rejects text past {@link MAX_VEGA_LITE_SPEC_CHARS}, JSON that isn't an object,
 * full Vega specs, external data URLs, oversized sequences, `sequence()` calls in
 * expressions and custom format types.
 *
 * :param source: The spec's JSON text, e.g. a fence body or file content.
 * :returns: The spec, or a message for the person viewing it.
 */
export function parseVegaLiteSpec(source: string): VegaLiteSpecResult {
  if (source.length > MAX_VEGA_LITE_SPEC_CHARS) {
    return {
      ok: false,
      error: `This chart spec is too large to draw (over ${MAX_VEGA_LITE_SPEC_CHARS.toLocaleString()} characters).`,
    };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return { ok: false, error: `This chart spec isn't valid JSON: ${detail}` };
  }
  if (!isPlainObject(parsed)) {
    return { ok: false, error: "A Vega-Lite spec must be a JSON object." };
  }
  const schema = parsed.$schema;
  if (typeof schema === "string" && /\/schema\/vega\/v\d/.test(schema)) {
    return { ok: false, error: "This is a Vega spec. Only Vega-Lite specs are drawn as charts." };
  }
  const reason = unsafeSpecReason(parsed);
  if (reason) return { ok: false, error: reason };
  return { ok: true, spec: parsed };
}
