// Brand-rule warnings for decks on a full design system: the regexes from the
// system's `_adherence.oxlintrc.json` selectors, reused on the deck's CSS. The
// file is untrusted, so patterns are length-capped, screened for catastrophic
// shapes, run only on short tokens, and in a worker with a time budget.

export const ADHERENCE_FILE = "_adherence.oxlintrc.json";
export const BRAND_MAX_PATTERNS = 8;
export const BRAND_MAX_PATTERN_LENGTH = 200;
const MAX_TOKEN = 64;
const MAX_DECLARATIONS = 5000;
const MAX_NAMES = 200;
const SELECTOR_REGEX = /\/((?:\\.|[^\\/\n])+)\/([a-z]*)/;
const GENERIC_FONTS = new Set(
  "serif sans-serif monospace cursive fantasy system-ui ui-serif ui-sans-serif ui-monospace ui-rounded emoji math fangsong inherit initial unset revert revert-layer".split(
    " ",
  ),
);

export interface BrandRules {
  patterns: RegExp[];
  tokens: string[];
  fonts: string[];
}

export interface BrandWarning {
  value: string;
  property: string;
  /** Where the value first appears, e.g. `<style> h1` or `slide 2 <p>`. */
  where: string;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const names = (v: unknown) =>
  (Array.isArray(v) ? v : isObject(v) ? Object.keys(v) : [])
    .filter((s): s is string => typeof s === "string" && !!s.trim() && s.length <= 80)
    .map((s) => s.trim())
    .slice(0, MAX_NAMES);

/** The regex in an AST selector, or null when it is missing, unsafe, or invalid. */
function compileSelector(selector: string): RegExp | null {
  // Font-family selectors target JS literals; fonts are checked against `fontFamilies`.
  if (/font-?family/i.test(selector)) return null;
  const [, source, flags] = selector.match(SELECTOR_REGEX) ?? [];
  if (!source || source.length > BRAND_MAX_PATTERN_LENGTH || /\\[1-9]/.test(source)) return null;
  const quantifiedGroup = /\)[+*{]/.test(source);
  const quantifiers = source.match(/[+*]|\{\d/g)?.length ?? 0;
  if (quantifiedGroup && (quantifiers > 1 || source.includes("|"))) return null;
  try {
    return new RegExp(source, flags.replace(/[^imsu]/g, ""));
  } catch {
    return null;
  }
}

/** Rules from the adherence file's text; null when it has none or is not valid JSON. */
export function parseAdherence(text: string): BrandRules | null {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isObject(raw)) return null;
  const rule = isObject(raw.rules) ? raw.rules["no-restricted-syntax"] : null;
  const patterns = (Array.isArray(rule) ? rule : [])
    .map((e) => (typeof e === "string" ? e : isObject(e) ? e.selector : null))
    .flatMap((s) => (typeof s === "string" ? (compileSelector(s) ?? []) : []))
    .slice(0, BRAND_MAX_PATTERNS);
  const omelette = isObject(raw["x-omelette"]) ? raw["x-omelette"] : {};
  const fonts = Array.isArray(omelette.fontFamilies) ? names(omelette.fontFamilies) : [];
  if (!patterns.length && !fonts.length) return null;
  return { patterns, tokens: names(omelette.tokens), fonts };
}

interface Finding extends BrandWarning {
  key: string;
}

const FUNCTION_SOURCE = String.raw`(?:var|url)\((?:[^()]|\([^()]*\))*\)`;
const HAS_FUNCTION = new RegExp(FUNCTION_SOURCE, "i");
const STRIP_FUNCTIONS = new RegExp(FUNCTION_SOURCE, "gi");
const unquote = (s: string) => s.trim().replace(/^(["'])(.*)\1$/, "$2");

function* declarationFindings(text: string, where: string, rules: BrandRules, budget: number[]) {
  for (const decl of text.split(";")) {
    if (budget[0]-- <= 0) return;
    const colon = decl.indexOf(":");
    const property = decl.slice(0, colon).trim().toLowerCase();
    if (colon < 0 || !property || property.startsWith("--")) continue;
    let value = decl.slice(colon + 1).replace(/!important/i, "");
    for (let i = 0; i < 4 && HAS_FUNCTION.test(value); i++) {
      value = value.replace(STRIP_FUNCTIONS, " ");
    }
    const found = (v: string): Finding => ({
      value: v,
      property,
      where,
      key: `${property}|${v.toLowerCase()}`,
    });
    if (property === "font-family") {
      if (!rules.fonts.length) continue;
      const allowed = new Set([...rules.fonts, ...rules.tokens].map((f) => f.toLowerCase()));
      for (const family of value.split(",").map(unquote)) {
        const f = family.toLowerCase();
        if (f && !GENERIC_FONTS.has(f) && !allowed.has(f)) yield found(family);
      }
      continue;
    }
    const tokens = value.replace(/"[^"]*"|'[^']*'/g, " ").split(/[\s,/()]+/);
    for (const token of tokens) {
      if (!token || token.length > MAX_TOKEN || /^0+(?:\.0+)?px$/i.test(token)) continue;
      if (/^1px$/i.test(token) && /^(?:border|outline)/.test(property)) continue;
      if (rules.patterns.some((p) => p.test(token))) yield found(token);
    }
  }
}

function* cssFindings(css: string, rules: BrandRules, budget: number[]) {
  const parts = css.replace(/\/\*[\s\S]*?\*\//g, "").split(/([{}])/);
  let selector = "";
  for (let i = 0; i < parts.length; i += 2) {
    if (parts[i + 1] === "{") selector = parts[i].replace(/\s+/g, " ").trim().slice(0, 60);
    else if (parts[i + 1] === "}") {
      yield* declarationFindings(parts[i], `<style> ${selector}`, rules, budget);
    }
  }
}

/** A stylesheet (`where` absent) or one element's `style` attribute. */
export interface StyleSource {
  text: string;
  where?: string;
}

/** `<style>` text and `style` attributes only, from an inert parse, in document order. */
export function styleSources(html: string): StyleSource[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const slides = [...doc.querySelectorAll("body > section")];
  const sources: StyleSource[] = [];
  for (const el of doc.querySelectorAll("style, [style]")) {
    if (el.tagName === "STYLE") sources.push({ text: el.textContent ?? "" });
    const style = el.getAttribute("style");
    if (style === null) continue;
    const slide = el.closest("body > section");
    const tag = `<${el.tagName.toLowerCase()}>`;
    sources.push({ text: style, where: slide ? `slide ${slides.indexOf(slide) + 1} ${tag}` : tag });
  }
  return sources;
}

function* sourceFindings(sources: StyleSource[], rules: BrandRules) {
  const budget = [MAX_DECLARATIONS];
  for (const { text, where } of sources) {
    yield* where === undefined
      ? cssFindings(text, rules, budget)
      : declarationFindings(text, where, rules, budget);
  }
}

/** Property and value pairs in the system's templates (one source list each), allowed in decks. */
export function templateBaseline(templates: StyleSource[][], rules: BrandRules): Set<string> {
  const pairs = new Set<string>();
  for (const sources of templates) for (const f of sourceFindings(sources, rules)) pairs.add(f.key);
  return pairs;
}

/** Each distinct raw value once, where it first appears, minus the template baseline. */
export function scanBrandWarnings(
  sources: StyleSource[],
  rules: BrandRules,
  baseline: ReadonlySet<string> = new Set(),
): BrandWarning[] {
  const seen = new Map<string, BrandWarning>();
  for (const { key, value, property, where } of sourceFindings(sources, rules)) {
    const id = value.toLowerCase();
    if (!baseline.has(key) && !seen.has(id)) seen.set(id, { value, property, where });
  }
  return [...seen.values()];
}

export interface BrandScanInput {
  adherence: string;
  deck: StyleSource[];
  templates: StyleSource[][];
}

/** The worker's whole job, pure; null when the adherence file has no rules. */
export function handleBrandScan(input: BrandScanInput): BrandWarning[] | null {
  const rules = parseAdherence(input.adherence);
  return rules && scanBrandWarnings(input.deck, rules, templateBaseline(input.templates, rules));
}

export const BRAND_SCAN_TIMEOUT_MS = 500;
type ScanWorker = Pick<Worker, "postMessage" | "terminate" | "onmessage" | "onerror">;

const spawnScanWorker = (): ScanWorker | null =>
  typeof Worker === "undefined"
    ? null
    : new Worker(new URL("./brandRules.worker.ts", import.meta.url), { type: "module" });

/**
 * Scan in a worker, since a hostile pattern can still backtrack for seconds.
 * Null (no badge) without a Worker, on error, or once `timeoutMs` passes.
 */
export function runBrandScan(
  input: BrandScanInput,
  timeoutMs = BRAND_SCAN_TIMEOUT_MS,
  spawn: () => ScanWorker | null = spawnScanWorker,
): Promise<BrandWarning[] | null> {
  let worker: ScanWorker | null;
  try {
    worker = spawn();
  } catch {
    worker = null;
  }
  if (!worker) return Promise.resolve(null);
  const w = worker;
  return new Promise((resolve) => {
    const done = (result: BrandWarning[] | null) => {
      clearTimeout(timer);
      w.terminate();
      resolve(result);
    };
    const timer = setTimeout(() => done(null), timeoutMs);
    w.onmessage = (e: MessageEvent) => done(Array.isArray(e.data) ? e.data : null);
    w.onerror = () => done(null);
    w.postMessage(input);
  });
}
