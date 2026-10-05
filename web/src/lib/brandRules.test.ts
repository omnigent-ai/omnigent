import { describe, expect, it, vi } from "vitest";
import { BrandScanThread } from "@/test/brandScanWorker";
import {
  BRAND_MAX_PATTERN_LENGTH,
  BRAND_MAX_PATTERNS,
  handleBrandScan,
  parseAdherence,
  runBrandScan,
  scanBrandWarnings,
  styleSources,
  templateBaseline,
  type BrandRules,
} from "./brandRules";

const HEX = { selector: "Literal[value=/#[0-9a-fA-F]{3,8}\\b/]", message: "Use a color token" };
const PX = { selector: "Literal[value=/^\\d+(\\.\\d+)?px$/]", message: "Use a size token" };
const FONT = {
  selector: "Property[key.name='fontFamily'] > Literal[value=/^(?!Fixture)/]",
  message: "Use a listed font",
};
const config = (patterns: unknown[] = [HEX, PX, FONT], omelette: unknown = undefined) =>
  JSON.stringify({
    rules: { "no-restricted-syntax": ["warn", ...patterns] },
    "x-omelette": omelette ?? { tokens: ["--fx-primary"], fontFamilies: ["Fixture Sans"] },
  });
const RULES = parseAdherence(config()) as BrandRules;
const deck = (style: string, body = "<section><h1>Hi</h1></section>") =>
  `<html><head><style>${style}</style></head><body>${body}</body></html>`;
const scan = (html: string, rules = RULES, baseline?: Set<string>) =>
  scanBrandWarnings(styleSources(html), rules, baseline);
const values = (html: string, rules = RULES, baseline?: Set<string>) =>
  scan(html, rules, baseline).map((w) => w.value);

describe("parseAdherence", () => {
  it("reads the value patterns, tokens, and fonts", () => {
    expect(RULES.patterns.map((p) => p.source)).toEqual([
      "#[0-9a-fA-F]{3,8}\\b",
      "^\\d+(\\.\\d+)?px$",
    ]);
    expect(RULES.tokens).toEqual(["--fx-primary"]);
    expect(RULES.fonts).toEqual(["Fixture Sans"]);
  });

  it.each([
    ["invalid JSON", "{"],
    ["a non-object", "[]"],
    ["no patterns or fonts", JSON.stringify({ rules: {} })],
  ])("has no rules for %s", (_label, text) => {
    expect(parseAdherence(text)).toBeNull();
  });

  it("drops an invalid regex and keeps the rest", () => {
    const rules = parseAdherence(config([{ selector: "Literal[value=/([a-z/]" }, HEX]))!;
    expect(rules.patterns.map((p) => p.source)).toEqual([HEX.selector.slice(15, -2)]);
  });

  it.each([
    ["nested quantifiers", "(a+)+$"],
    ["a quantified alternation", "(a|aa)*$"],
    ["a backreference", "(a)\\1"],
    ["an overlong pattern", `a{1}${"b".repeat(BRAND_MAX_PATTERN_LENGTH)}`],
  ])("drops a pattern with %s", (_label, source) => {
    const rules = parseAdherence(config([{ selector: `Literal[value=/${source}/]` }, HEX]))!;
    expect(rules.patterns).toHaveLength(1);
  });

  it("skips only font-family selectors, keeping other font rules", () => {
    const fontSize = { selector: "Property[key.name='fontSize'] > Literal[value=/^\\d+px$/]" };
    const fontFamily = { selector: "Property[key.name='font-family'] > Literal[value=/x/]" };
    const rules = parseAdherence(config([fontSize, fontFamily, FONT]))!;
    expect(rules.patterns.map((p) => p.source)).toEqual(["^\\d+px$"]);
  });

  it("ignores fields of the wrong shape", () => {
    const rules = parseAdherence(config([HEX, 7, null], { tokens: 3, fontFamilies: [1, "Ok"] }))!;
    expect(rules).toMatchObject({ tokens: [], fonts: ["Ok"] });
  });

  it("compiles at most BRAND_MAX_PATTERNS patterns from a large rules file", () => {
    const many = Array.from({ length: 20 }, (_, i) => ({
      selector: `Literal[value=/^p${i}$/]`,
    }));
    const spy = vi.spyOn(globalThis, "RegExp");
    const before = spy.mock.calls.length;
    const rules = parseAdherence(config(many))!;
    const compiles = spy.mock.calls.length - before;
    spy.mockRestore();
    expect(rules.patterns).toHaveLength(BRAND_MAX_PATTERNS);
    expect(compiles).toBe(BRAND_MAX_PATTERNS);
  });
});

describe("scanBrandWarnings", () => {
  it("flags raw hex colors, pixel sizes, and unlisted fonts", () => {
    const html = deck("h1{color:#FF0000;margin:12px;font-family:'Comic Sans', Fixture Sans}");
    expect(scan(html)).toEqual([
      { value: "#FF0000", property: "color", where: "<style> h1" },
      { value: "12px", property: "margin", where: "<style> h1" },
      { value: "Comic Sans", property: "font-family", where: "<style> h1" },
    ]);
  });

  it("scans style attributes and names the slide", () => {
    const html = deck("", '<section></section><section><p style="color:#123456">x</p></section>');
    expect(scan(html)).toEqual([{ value: "#123456", property: "color", where: "slide 2 <p>" }]);
  });

  it("never scans slide text or scripts", () => {
    const body =
      "<section><p>#ff0000 12px font-family:Comic</p><script>x='#abcdef'</script></section>";
    expect(values(deck("", body))).toEqual([]);
  });

  it("ignores custom property definitions and var() fallbacks", () => {
    expect(values(deck(":root{--x:#ff0000;--y:12px}h1{color:var(--x, #00ff00)}"))).toEqual([]);
  });

  it("ignores 0px and 1px border/outline widths, but not radius, offset, or padding", () => {
    expect(values(deck("h1{margin:0px;border:1px solid var(--x);outline-width:1px}"))).toEqual([]);
    expect(scan(deck("h1{border-radius:1px}"))).toEqual([
      { value: "1px", property: "border-radius", where: "<style> h1" },
    ]);
    expect(scan(deck("h1{outline-offset:1px}"))).toEqual([
      { value: "1px", property: "outline-offset", where: "<style> h1" },
    ]);
    expect(scan(deck("h1{padding:1px}"))).toEqual([
      { value: "1px", property: "padding", where: "<style> h1" },
    ]);
  });

  it("allows generic families and system tokens as fonts", () => {
    expect(values(deck("h1{font-family:--fx-primary, sans-serif, inherit}"))).toEqual([]);
  });

  it("counts each distinct value once, keeping where it first appears", () => {
    const html = deck("h1{color:#abc}h2{background:#ABC}", '<p style="color:#abc">x</p>');
    expect(scan(html)).toEqual([{ value: "#abc", property: "color", where: "<style> h1" }]);
  });

  it("allows property and value pairs from the template baseline", () => {
    const baseline = templateBaseline(
      [styleSources(deck(".t{color:#0B5FFF;margin:24px}")), [{ text: ".g{gap:16px}" }]],
      RULES,
    );
    const html = deck("h1{color:#0b5fff;gap:16px;margin:16px;padding:24px}");
    expect(values(html, RULES, baseline)).toEqual(["16px", "24px"]);
  });

  it("does not flag fonts without a fontFamilies list", () => {
    const rules = parseAdherence(config([HEX], { tokens: [] }))!;
    expect(values(deck("h1{font-family:Comic Sans;color:#fff}"), rules)).toEqual(["#fff"]);
  });

  it("skips overlong tokens so a slow pattern only sees short input", () => {
    const rules = parseAdherence(config([{ selector: "Literal[value=/^a+b$/]" }]))!;
    expect(values(deck(`h1{content:${"a".repeat(70)}b;x:aab}`), rules)).toEqual(["aab"]);
  });
});

describe("runBrandScan", () => {
  const CATASTROPHIC = { selector: "Literal[value=/^\\d*\\d*\\d*\\d*\\d*\\d*\\d*\\d*x$/]" };
  const input = (adherence: string, html: string) => ({
    adherence,
    deck: styleSources(html),
    templates: [],
  });

  it("is the pure scan in the worker: parse, baseline, then warnings", () => {
    const result = handleBrandScan({
      adherence: config(),
      deck: styleSources(deck("h1{color:#0b5fff;margin:12px}")),
      templates: [[{ text: ".t{color:#0B5FFF}" }]],
    });
    expect(result?.map((w) => w.value)).toEqual(["12px"]);
    expect(handleBrandScan({ adherence: "{", deck: [], templates: [] })).toBeNull();
  });

  it("scans in a worker thread", async () => {
    const thread = new BrandScanThread();
    const result = await runBrandScan(input(config(), deck("h1{color:#ff0000}")), {
      timeoutMs: 30_000,
      spawn: () => thread,
    });
    expect(result).toEqual([{ value: "#ff0000", property: "color", where: "<style> h1" }]);
    expect(thread.terminated).toBe(true);
  });

  it("terminates a catastrophic pattern and resolves null without blocking", async () => {
    const adherence = config([CATASTROPHIC]);
    // The static screen lets this one through; the worker budget is what stops it.
    expect(parseAdherence(adherence)!.patterns).toHaveLength(1);
    const thread = new BrandScanThread();
    let ticks = 0;
    const tick = setInterval(() => ticks++, 20);
    const started = performance.now();
    const result = await runBrandScan(input(adherence, deck(`h1{width:${"1".repeat(40)}}`)), {
      timeoutMs: 500,
      spawn: () => thread,
    });
    clearInterval(tick);
    expect(result).toBeNull();
    expect(thread.terminated).toBe(true);
    expect(performance.now() - started).toBeLessThan(5000);
    expect(ticks).toBeGreaterThan(5);
  });

  it("aborts mid-scan by terminating the worker before the 500 ms budget", async () => {
    const adherence = config([CATASTROPHIC]);
    const thread = new BrandScanThread();
    const abort = new AbortController();
    const started = performance.now();
    const pending = runBrandScan(input(adherence, deck(`h1{width:${"1".repeat(40)}}`)), {
      timeoutMs: 30_000,
      spawn: () => thread,
      signal: abort.signal,
    });
    await new Promise<void>((r) => {
      setTimeout(r, 40);
    });
    abort.abort();
    expect(await pending).toBeNull();
    expect(thread.terminated).toBe(true);
    expect(performance.now() - started).toBeLessThan(500);
  });

  it("resolves null without a Worker instead of scanning on the main thread", async () => {
    expect(typeof Worker).toBe("undefined");
    expect(await runBrandScan(input(config(), deck("h1{color:#ff0000}")))).toBeNull();
  });
});
