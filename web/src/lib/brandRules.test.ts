import { describe, expect, it } from "vitest";
import {
  BRAND_MAX_PATTERN_LENGTH,
  parseAdherence,
  scanBrandWarnings,
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
const values = (html: string, rules = RULES, baseline?: Set<string>) =>
  scanBrandWarnings(html, rules, baseline).map((w) => w.value);

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

  it("ignores fields of the wrong shape", () => {
    const rules = parseAdherence(config([HEX, 7, null], { tokens: 3, fontFamilies: [1, "Ok"] }))!;
    expect(rules).toMatchObject({ tokens: [], fonts: ["Ok"] });
  });
});

describe("scanBrandWarnings", () => {
  it("flags raw hex colors, pixel sizes, and unlisted fonts", () => {
    const html = deck("h1{color:#FF0000;margin:12px;font-family:'Comic Sans', Fixture Sans}");
    expect(scanBrandWarnings(html, RULES)).toEqual([
      { value: "#FF0000", property: "color", where: "<style> h1" },
      { value: "12px", property: "margin", where: "<style> h1" },
      { value: "Comic Sans", property: "font-family", where: "<style> h1" },
    ]);
  });

  it("scans style attributes and names the slide", () => {
    const html = deck("", '<section></section><section><p style="color:#123456">x</p></section>');
    expect(scanBrandWarnings(html, RULES)).toEqual([
      { value: "#123456", property: "color", where: "slide 2 <p>" },
    ]);
  });

  it("never scans slide text or scripts", () => {
    const body =
      "<section><p>#ff0000 12px font-family:Comic</p><script>x='#abcdef'</script></section>";
    expect(values(deck("", body))).toEqual([]);
  });

  it("ignores custom property definitions and var() fallbacks", () => {
    expect(values(deck(":root{--x:#ff0000;--y:12px}h1{color:var(--x, #00ff00)}"))).toEqual([]);
  });

  it("ignores 0px and 1px borders and outlines, but not other 1px values", () => {
    const css = "h1{margin:0px;border:1px solid var(--x);outline-width:1px;padding:1px}";
    expect(scanBrandWarnings(deck(css), RULES)).toEqual([
      { value: "1px", property: "padding", where: "<style> h1" },
    ]);
  });

  it("allows generic families and system tokens as fonts", () => {
    expect(values(deck("h1{font-family:--fx-primary, sans-serif, inherit}"))).toEqual([]);
  });

  it("counts each distinct value once, keeping where it first appears", () => {
    const html = deck("h1{color:#abc}h2{background:#ABC}", '<p style="color:#abc">x</p>');
    expect(scanBrandWarnings(html, RULES)).toEqual([
      { value: "#abc", property: "color", where: "<style> h1" },
    ]);
  });

  it("allows property and value pairs from the template baseline", () => {
    const baseline = templateBaseline(
      [
        { path: "templates/title.html", text: deck(".t{color:#0B5FFF;margin:24px}") },
        { path: "templates/grid.css", text: ".g{gap:16px}" },
      ],
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
