// Draws Vega-Lite specs with Vega. The libraries load on the first chart, so
// they stay out of the main bundle; this module imports only their types.

import type * as VegaModule from "vega";
import type { expressionInterpreter as ExpressionInterpreter } from "vega-interpreter";
import type { compile as CompileVegaLite, Config as VegaLiteConfig, TopLevelSpec } from "vega-lite";
import { getEmbedRoot } from "@/lib/host";
import { isOversizedFormat, MAX_VEGA_LITE_FORMAT_WIDTH, type VegaLiteSpec } from "./vegaLiteSpec";

interface VegaModules {
  vega: typeof VegaModule;
  compile: typeof CompileVegaLite;
  expressionInterpreter: typeof ExpressionInterpreter;
}

let modulesPromise: Promise<VegaModules> | null = null;

/** Most items `sequence()` and characters `pad()` may produce in a chart expression. */
export const MAX_EXPRESSION_ITEMS = 100_000;

/** Thrown by a chart expression that asks for more than the limits allow. */
export class ChartLimitError extends Error {}

type ExpressionFunction = ((this: unknown, ...args: unknown[]) => unknown) & {
  [LIMITED]?: true;
};
interface TimeInterval {
  count?: (start: Date, end: Date) => number;
}

// Marks a wrapped function, so reloading this module doesn't wrap it twice.
const LIMITED = Symbol.for("omnigent.chartExpressionLimit");

/** Wrap expression function *name* so a call *check* refuses throws instead of running. */
function limitExpressionFunction(
  vega: typeof VegaModule,
  name: string,
  check: (args: unknown[]) => string | null,
): void {
  const original = vega.expressionFunction(name) as ExpressionFunction | undefined;
  if (!original || original[LIMITED]) return;
  // Keeps the original's `this`: `format` reads the view's locale from it.
  const limited: ExpressionFunction = function (this: unknown, ...args: unknown[]) {
    const reason = check(args);
    if (reason) throw new ChartLimitError(reason);
    return original.apply(this, args);
  };
  limited[LIMITED] = true;
  vega.expressionFunction(name, limited);
}

const tooMany = (what: string) =>
  `${what} can produce at most ${MAX_EXPRESSION_ITEMS.toLocaleString()} items.`;

/**
 * Cap the expression functions that allocate in proportion to their arguments.
 * This runs at evaluation time, so it holds however a spec spells the call.
 */
function installExpressionLimits(vega: typeof VegaModule): void {
  limitExpressionFunction(vega, "sequence", (args) => {
    const [start, stop, step = 1] = (args.length === 1 ? [0, args[0]] : args).map(Number);
    return step !== 0 && Math.ceil((stop - start) / step) > MAX_EXPRESSION_ITEMS
      ? tooMany("sequence()")
      : null;
  });
  limitExpressionFunction(vega, "pad", ([text, length, character]) => {
    const missing = Math.max(0, Number(length) - String(text).length);
    return missing * String(character || " ").length > MAX_EXPRESSION_ITEMS
      ? `pad() can produce at most ${MAX_EXPRESSION_ITEMS.toLocaleString()} characters.`
      : null;
  });
  limitExpressionFunction(vega, "format", ([, specifier]) =>
    isOversizedFormat(String(specifier))
      ? `format() widths can be at most ${MAX_VEGA_LITE_FORMAT_WIDTH.toLocaleString()}.`
      : null,
  );
  const intervals = vega as unknown as Record<string, (unit: unknown) => TimeInterval | undefined>;
  for (const [name, intervalOf] of [
    ["timeSequence", intervals.timeInterval],
    ["utcSequence", intervals.utcInterval],
  ] as const) {
    limitExpressionFunction(vega, name, ([unit, start, stop, step = 1]) => {
      const count = intervalOf(unit)?.count?.(new Date(Number(start)), new Date(Number(stop)));
      return count !== undefined && count / Math.max(1, Number(step)) > MAX_EXPRESSION_ITEMS
        ? tooMany(`${name}()`)
        : null;
    });
  }
}

/** Load Vega, Vega-Lite and the expression interpreter once; a failed load retries next time. */
export function loadVegaModules(): Promise<VegaModules> {
  modulesPromise ??= Promise.all([
    import("vega"),
    import("vega-lite"),
    import("vega-interpreter"),
  ]).then(
    ([vega, vegaLite, interpreter]) => {
      installExpressionLimits(vega);
      return {
        vega,
        compile: vegaLite.compile,
        expressionInterpreter: interpreter.expressionInterpreter,
      };
    },
    (error: unknown) => {
      modulesPromise = null;
      throw error;
    },
  );
  return modulesPromise;
}

/** App theme colors a chart draws with, as CSS color strings. */
export interface ChartColors {
  foreground: string;
  mutedForeground: string;
  border: string;
  background: string;
  accent: string;
  fontFamily: string;
}

const LIGHT_FALLBACK: ChartColors = {
  foreground: "#27272a",
  mutedForeground: "#71717a",
  border: "#e4e4e7",
  background: "#ffffff",
  accent: "#3b8ff5",
  fontFamily: "sans-serif",
};

const DARK_FALLBACK: ChartColors = {
  foreground: "#f5f5f4",
  mutedForeground: "#a8a29e",
  border: "#3f3f46",
  background: "#18181b",
  accent: "#3b8ff5",
  fontFamily: "sans-serif",
};

/** Read the theme's colors from the CSS variables in effect at *element*. */
export function readChartColors(element: Element, dark: boolean): ChartColors {
  const fallback = dark ? DARK_FALLBACK : LIGHT_FALLBACK;
  const style = getComputedStyle(element);
  const variable = (name: string, fallbackValue: string) =>
    style.getPropertyValue(name).trim() || fallbackValue;
  return {
    foreground: variable("--foreground", fallback.foreground),
    mutedForeground: variable("--muted-foreground", fallback.mutedForeground),
    border: variable("--border", fallback.border),
    background: variable("--background", fallback.background),
    accent: variable("--chart-1", fallback.accent),
    fontFamily: style.fontFamily || fallback.fontFamily,
  };
}

/**
 * Vega-Lite config that draws text, axes and legends in the app's colors on a
 * transparent canvas. A spec's own `config` overrides it.
 */
export function chartThemeConfig(colors: ChartColors): VegaLiteConfig {
  const text = { labelColor: colors.mutedForeground, titleColor: colors.foreground };
  return {
    background: "transparent",
    font: colors.fontFamily,
    mark: { color: colors.accent },
    axis: {
      ...text,
      domainColor: colors.border,
      gridColor: colors.border,
      tickColor: colors.border,
    },
    legend: text,
    header: text,
    title: { color: colors.foreground, subtitleColor: colors.mutedForeground },
    view: { stroke: colors.border },
  };
}

/**
 * Fill the container's width when a single-view spec doesn't set one. Composite
 * specs (layer, concat, facet) keep Vega-Lite's own sizing.
 */
export function withContainerWidth(spec: VegaLiteSpec): VegaLiteSpec {
  if (spec.width !== undefined || typeof spec.mark === "undefined") return spec;
  return { ...spec, width: "container" };
}

const BLOCKED_LOAD = "Charts can only use inline data.";

/** A Vega loader that refuses every file, URL and image, and gives links no target. */
export function blockedLoader(): VegaModule.Loader {
  const refuse = () => Promise.reject(new Error(BLOCKED_LOAD));
  return {
    load: refuse,
    // A link without a target: a click does nothing and SVG export leaves it out.
    sanitize: (_uri, options) =>
      options?.context === "href" ? Promise.resolve({} as { href: string }) : refuse(),
    http: refuse,
    file: refuse,
  };
}

/**
 * Event rules every chart runs under, whatever its own config says: inputs stay in
 * the chart, and it can't listen to page elements, keys or timers.
 */
export const CONTAINED_EVENTS = {
  bind: "container",
  selector: false,
  // Resize, plus the pointer events Vega-Lite's drag selections and panning use.
  window: ["resize", "pointermove", "pointerup"],
  timer: false,
  globalCursor: false,
} as const;

const CURSOR_KEYWORD = /^[a-z-]+$/i;

/** *text* with CSS escapes (e.g. ``\72`` for ``r``) decoded. */
function decodeCssEscapes(text: string): string {
  return text.replace(/\\([0-9a-f]{1,6})\s?|\\(.)/gi, (_match, hex?: string, char?: string) => {
    if (char !== undefined) return char;
    const codePoint = Number.parseInt(hex!, 16);
    return codePoint > 0 && codePoint <= 0x10ffff ? String.fromCodePoint(codePoint) : "\ufffd";
  });
}

function isKeywordCursor(cursor: unknown): boolean {
  if (typeof cursor === "string") return CURSOR_KEYWORD.test(cursor);
  if (typeof cursor !== "object" || cursor === null || Array.isArray(cursor)) return false;
  const keys = Object.keys(cursor);
  const { value } = cursor as Record<string, unknown>;
  return keys.length === 1 && typeof value === "string" && CURSOR_KEYWORD.test(value);
}

/**
 * The first reason a compiled Vega spec could load a remote file, or null.
 * A CSS `url(...)` in a color or cursor would load one, and so would a cursor
 * computed from data or a `cursor` signal.
 */
export function unsafeCompiledReason(spec: unknown): string | null {
  const pending: [key: string, value: unknown][] = [["", spec]];
  while (pending.length > 0) {
    const [key, node] = pending.pop()!;
    if (typeof node === "string") {
      if (/url\(/i.test(decodeCssEscapes(node))) {
        return "Chart text can't contain url(...), which would load a remote file.";
      }
      continue;
    }
    if (Array.isArray(node)) {
      for (const item of node) {
        if (key === "signals" && (item as { name?: unknown } | null)?.name === "cursor") {
          return "A chart can't define a `cursor` parameter.";
        }
        pending.push([key, item]);
      }
      continue;
    }
    if (typeof node !== "object" || node === null) continue;
    for (const [childKey, value] of Object.entries(node)) {
      if (childKey === "cursor" && !isKeywordCursor(value)) {
        return "A mark's cursor must be a CSS keyword such as `pointer`.";
      }
      pending.push([childKey, value]);
    }
  }
  return null;
}

const MAX_TOOLTIP_VALUE_CHARS = 500;

function tooltipText(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > MAX_TOOLTIP_VALUE_CHARS
    ? `${text.slice(0, MAX_TOOLTIP_VALUE_CHARS)}…`
    : text;
}

function tooltipContent(value: unknown): HTMLElement {
  const content = document.createElement("div");
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    content.textContent = Array.isArray(value)
      ? value.map(tooltipText).join(", ")
      : tooltipText(value);
    return content;
  }
  // `image` would load a URL, so it is left out like any other remote resource.
  const { title, image: _image, ...fields } = value as Record<string, unknown>;
  if (title !== undefined) {
    const heading = document.createElement("div");
    heading.className = "mb-1 font-medium";
    heading.textContent = tooltipText(title);
    content.append(heading);
  }
  const table = document.createElement("table");
  for (const [key, field] of Object.entries(fields)) {
    if (field === undefined) continue;
    const row = table.insertRow();
    const keyCell = row.insertCell();
    keyCell.className = "pr-2 text-right align-top text-muted-foreground";
    keyCell.textContent = key;
    const valueCell = row.insertCell();
    valueCell.className = "align-top break-words";
    valueCell.textContent = tooltipText(field);
  }
  if (table.rows.length > 0) content.append(table);
  return content;
}

const TOOLTIP_OFFSET = 10;

/**
 * A Vega tooltip handler that shows values as plain text in a themed popover.
 * Returns the handler and a function that removes its element.
 */
export function createChartTooltip(): {
  handler: VegaModule.TooltipHandler;
  remove: () => void;
} {
  let element: HTMLDivElement | null = null;
  const handler: VegaModule.TooltipHandler = (_handler, event, _item, value) => {
    if (value == null || value === "") {
      element?.remove();
      element = null;
      return;
    }
    if (!element) {
      element = document.createElement("div");
      element.setAttribute("role", "tooltip");
      element.dataset.testid = "chart-tooltip";
      element.className =
        "pointer-events-none fixed z-50 max-w-xs rounded-md border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-md";
      (getEmbedRoot() ?? document.body).append(element);
    }
    element.replaceChildren(tooltipContent(value));
    const box = element.getBoundingClientRect();
    const left =
      event.clientX + TOOLTIP_OFFSET + box.width > window.innerWidth
        ? event.clientX - TOOLTIP_OFFSET - box.width
        : event.clientX + TOOLTIP_OFFSET;
    const top =
      event.clientY + TOOLTIP_OFFSET + box.height > window.innerHeight
        ? event.clientY - TOOLTIP_OFFSET - box.height
        : event.clientY + TOOLTIP_OFFSET;
    element.style.left = `${Math.max(0, left)}px`;
    element.style.top = `${Math.max(0, top)}px`;
  };
  return {
    handler,
    remove: () => {
      element?.remove();
      element = null;
    },
  };
}

/** A drawn chart. */
export interface RenderedChart {
  /** The chart as an image, on the theme's background so it reads outside the app. */
  toBlob: (format: "png" | "svg") => Promise<Blob>;
  /** Stop the view and remove its listeners and tooltip. */
  finalize: () => void;
}

const PNG_SCALE = 2;

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("PNG export failed."))));
  });
}

/** Thrown when a draw is cancelled before its view is built. */
export class ChartDrawCancelled extends Error {}

/**
 * Compile *spec* and draw it into *container* as SVG.
 *
 * Data, images and links can't load (the loader refuses them), expressions run
 * in Vega's interpreter rather than as generated code, and events are confined
 * to the chart (see :data:`CONTAINED_EVENTS`).
 *
 * :param container: Element the chart draws into; its contents are replaced.
 * :param spec: A spec accepted by ``parseVegaLiteSpec``.
 * :param readColors: Reads theme colors once the libraries load, e.g. :func:`readChartColors`.
 * :param isCancelled: Checked before the view is built; a cancelled draw throws
 *   :class:`ChartDrawCancelled` and leaves *container* alone.
 * :raises Error: When the spec is refused, doesn't compile, or the libraries fail to load.
 */
export async function renderVegaLiteChart(
  container: HTMLElement,
  spec: VegaLiteSpec,
  readColors: () => ChartColors,
  isCancelled: () => boolean = () => false,
): Promise<RenderedChart> {
  const { vega, compile, expressionInterpreter } = await loadVegaModules();
  if (isCancelled()) throw new ChartDrawCancelled();
  // Read after the await, once the app has applied a theme change.
  const colors = readColors();
  const compiled = compile(withContainerWidth(spec) as unknown as TopLevelSpec, {
    config: chartThemeConfig(colors),
  });
  const unsafe = unsafeCompiledReason(compiled.spec);
  if (unsafe) throw new Error(unsafe);
  // The spec's own config wins over parse's, so the event rules go into it.
  compiled.spec.config = {
    ...compiled.spec.config,
    events: CONTAINED_EVENTS as unknown as VegaModule.Config["events"],
  };
  const runtime = vega.parse(compiled.spec, undefined, { ast: true });
  // A fresh element per draw, so a superseded view never shares this one's SVG.
  const target = document.createElement("div");
  target.className = "w-full";
  container.replaceChildren(target);
  const tooltip = createChartTooltip();
  // Vega logs expression errors rather than throwing; a limit hit while drawing fails the chart.
  const logger = vega.logger(vega.Error);
  const logError = logger.error.bind(logger);
  let limitHit: ChartLimitError | undefined;
  let firstRun = true;
  logger.error = (...args: readonly unknown[]) => {
    if (firstRun) {
      limitHit ??= args.find((arg): arg is ChartLimitError => arg instanceof ChartLimitError);
    }
    return logError(...args);
  };
  const view = new vega.View(runtime, {
    container: target,
    renderer: "svg",
    hover: true,
    loader: blockedLoader(),
    expr: expressionInterpreter,
    tooltip: tooltip.handler,
    logger,
  });
  const finalize = () => {
    view.finalize();
    tooltip.remove();
  };
  try {
    await view.runAsync();
    firstRun = false;
    if (limitHit !== undefined) throw new ChartLimitError(limitHit.message);
  } catch (error) {
    finalize();
    throw error;
  }

  // A spec that sets its own background keeps it in downloads too.
  const shownBackground = view.background();
  const exportBackground =
    shownBackground && shownBackground !== "transparent" ? shownBackground : colors.background;
  const exportImage = async (format: "png" | "svg"): Promise<Blob> => {
    view.background(exportBackground);
    try {
      return format === "svg"
        ? new Blob([await view.toSVG()], { type: "image/svg+xml" })
        : await canvasBlob(await view.toCanvas(PNG_SCALE));
    } finally {
      view.background(shownBackground);
      await view.runAsync();
    }
  };
  // One export at a time, so overlapping clicks can't leave the export background shown.
  let exports: Promise<unknown> = Promise.resolve();

  return {
    toBlob: (format) => {
      const result = exports.then(() => exportImage(format));
      exports = result.catch(() => undefined);
      return result;
    },
    finalize,
  };
}
