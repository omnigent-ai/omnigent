/// <reference types="node" />
// Node types via explicit reference: the app tsconfig is browser-only, and
// importing index.css?raw instead yields "" under vitest's CSS stubbing.
import { readFileSync } from "node:fs";
// lightningcss is the minifier @tailwindcss/vite runs during `vite build`
// (resolved from its dependency tree, so we test the version the build uses).
import { transform } from "lightningcss";
import { describe, expect, it } from "vitest";
import { PALETTES } from "./lib/themePalette";

// Relative to the vitest root (web/) — import.meta.url is not a file://
// URL inside vitest's module graph, so it can't locate the file.
const cssSource = readFileSync("src/index.css", "utf8");

function customPropertiesFor(selector: string): Map<string, string> {
  const start = cssSource.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`Missing CSS rule: ${selector}`);
  const bodyStart = cssSource.indexOf("{", start) + 1;
  const bodyEnd = cssSource.indexOf("}", bodyStart);
  return new Map(
    [...cssSource.slice(bodyStart, bodyEnd).matchAll(/--([\w-]+):\s*([^;]+);/g)].map(
      ([, name, value]) => [name, value.trim()],
    ),
  );
}

function normalizeHex(value: string): string {
  return value.replace(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/i, "#$1$1$2$2$3$3").toLowerCase();
}

describe("index.css semantic token contract", () => {
  const light = customPropertiesFor(":root");
  const dark = customPropertiesFor(".dark");
  const omni = PALETTES.find((palette) => palette.id === "omni")!;

  it.each([
    "success-foreground",
    "warning-foreground",
    "info-foreground",
    "file-reference",
    "landing-footer-foreground",
    "otto-pink",
    "otto-green",
  ])("defines %s in both default modes", (token) => {
    expect(light.has(token)).toBe(true);
    expect(dark.has(token)).toBe(true);
  });

  it("keeps the Omni palette preview aligned with real surface and brand tokens", () => {
    expect(normalizeHex(omni.light.bg)).toBe(normalizeHex(light.get("background")!));
    expect(normalizeHex(omni.light.card)).toBe(normalizeHex(light.get("card-solid")!));
    expect(normalizeHex(omni.light.accent)).toBe(normalizeHex(light.get("brand-accent")!));
    expect(normalizeHex(omni.dark.bg)).toBe(normalizeHex(dark.get("background")!));
    expect(normalizeHex(omni.dark.card)).toBe(normalizeHex(dark.get("card-solid")!));
    expect(normalizeHex(omni.dark.accent)).toBe(normalizeHex(dark.get("brand-accent")!));
  });

  it("exposes the named transcript typography scale", () => {
    expect(cssSource).toContain("--text-card-title: 13px;");
    expect(cssSource).toContain("--text-card-body: 12px;");
    expect(cssSource).toContain("--text-card-meta: 11px;");
  });

  it("matches the reference landing-footer foregrounds in both default modes", () => {
    expect(light.get("landing-footer-foreground")).toBe("#71717a");
    expect(dark.get("landing-footer-foreground")).toBe("#92a4b3");
    expect(cssSource).toContain("--color-landing-footer: var(--landing-footer-foreground);");
  });
});

/* Regression test for the "transparent dropdown in prod" bug.
 *
 * Dark mode renders popovers/cards with a semi-transparent background that
 * relies on `backdrop-filter` glass rules in index.css. LightningCSS
 * collapses an unprefixed + `-webkit-` declaration pair into a single
 * logical declaration, keeping only the LAST one written. With the
 * unprefixed property first, the built CSS ended up with only
 * `-webkit-backdrop-filter` — which Chrome ignores — so menus turned
 * see-through in `npm run build` output while `npm run dev` looked fine.
 *
 * This test minifies the actual glass rules from index.css the same way
 * the build does and fails if either form of backdrop-filter is lost.
 */

// Tailwind v4 browser baseline (Safari 16.4, Chrome 111, Firefox 128),
// mirroring the targets the build minifies against. Safari <18 needs the
// -webkit- prefix for backdrop-filter; Chrome/Firefox need it unprefixed.
const TARGETS = {
  safari: (16 << 16) | (4 << 8),
  chrome: 111 << 16,
  firefox: 128 << 16,
};

// Matches `backdrop-filter:` declarations but not `-webkit-backdrop-filter:`.
const UNPREFIXED_DECL = /(?<![-\w])backdrop-filter\s*:/;
const WEBKIT_DECL = /-webkit-backdrop-filter\s*:/;

/** Innermost `selector { ... }` blocks that declare backdrop-filter. */
function extractBackdropFilterRules(css: string): string[] {
  const blocks = css.match(/[^{}]+\{[^{}]*\}/g) ?? [];
  // Require a `:` so blocks that merely mention backdrop-filter in a
  // comment (e.g. the dark-token block) are not treated as glass rules.
  return blocks.filter((block) => UNPREFIXED_DECL.test(block));
}

describe("index.css backdrop-filter glass rules", () => {
  const rules = extractBackdropFilterRules(cssSource);

  it("has the glass rules this test exists to protect", () => {
    // 2 today: the bg-card frosted surfaces and the popover/menu rule.
    // 0 or 1 means a rule was removed/renamed — update or delete this test.
    expect(rules.length).toBeGreaterThanOrEqual(2);
  });

  it.each(rules.map((rule) => [rule.trim().slice(0, 60), rule] as const))(
    "keeps both backdrop-filter forms after build minification: %s",
    (_label, rule) => {
      const minified = new TextDecoder().decode(
        transform({
          filename: "index.css",
          code: new TextEncoder().encode(rule),
          minify: true,
          targets: TARGETS,
        }).code,
      );

      // Chrome/Firefox only honor the unprefixed property. Losing it is the
      // exact prod-only transparency bug: LightningCSS keeps the last of a
      // prefixed/unprefixed pair, so `-webkit-` must be declared FIRST.
      expect(minified, "unprefixed backdrop-filter was dropped by minification").toMatch(
        UNPREFIXED_DECL,
      );
      // Safari 16.4-17 only honor the -webkit- form; it must survive too.
      expect(minified, "-webkit-backdrop-filter was dropped by minification").toMatch(WEBKIT_DECL);
    },
  );
});

/* Regression test for the "page gets wider when the kebab menu opens" bug.
 *
 * The bg-card glass rule used to exclude `[aria-hidden="true"]` to skip
 * visually collapsed panels. But Radix's modal a11y hiding sets
 * aria-hidden="true" on the OPEN sidebar while a menu/dialog is up, which
 * dropped the rule's 1px border and reflowed every sidebar row 2px wider
 * (titles gained a character). The rule now keys on `data-collapsed`,
 * which only the panels themselves set. This test runs the actual selector
 * from index.css against a real DOM to pin that contract.
 */
describe("index.css bg-card glass rule selector", () => {
  // The selector of the rule declaring the bg-card glass border/blur.
  const cardRule = extractBackdropFilterRules(cssSource).find((rule) => rule.includes(".bg-card"))!;
  // Strip comments preceding the selector in the extracted block.
  const selector = cardRule
    .slice(0, cardRule.indexOf("{"))
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();

  function makeAside(): HTMLElement {
    const dark = document.createElement("div");
    dark.className = "dark";
    const aside = document.createElement("aside");
    aside.className = "conversations-sidebar flex flex-col bg-card";
    dark.appendChild(aside);
    document.body.appendChild(dark);
    return aside;
  }

  it("matches an open bg-card panel even while Radix marks it aria-hidden", () => {
    const aside = makeAside();
    // Open panel: glass border applies.
    expect(aside.matches(selector)).toBe(true);
    // Radix hideOthers sets aria-hidden="true" on open panels whenever a
    // modal menu/dialog is up. The glass styling must NOT react to it —
    // if this fails, opening the session kebab menu drops the sidebar's
    // 1px border again and every row reflows 2px wider.
    aside.setAttribute("aria-hidden", "true");
    expect(aside.matches(selector)).toBe(true);
    aside.remove();
  });

  it("stops matching when the panel marks itself collapsed", () => {
    const aside = makeAside();
    // Closed panels (w-0) set data-collapsed; the glass border/shadow must
    // not paint them as a glowing strip along the screen edge.
    aside.setAttribute("data-collapsed", "true");
    expect(aside.matches(selector)).toBe(false);
    aside.remove();
  });
});

describe("index.css default light composer action", () => {
  it("uses the soft pink fill and rose outline without changing the darker global primary", () => {
    expect(cssSource).toContain("--primary: #d4387f;");
    expect(cssSource).toMatch(
      /:root:not\(\.dark\):not\(\[data-theme\]\)[\s\S]*?new-chat-landing-submit[\s\S]*?border-color: #c94f89 !important;[\s\S]*?background: #e468a1 !important;/,
    );
  });
});

describe("index.css text selection", () => {
  it("uses restrained macOS system-blue washes instead of the brand accent", () => {
    expect(cssSource).toContain("background: rgba(0, 122, 255, 0.22);");
    expect(cssSource).toContain("background: rgba(10, 132, 255, 0.28);");
    expect(cssSource).not.toContain("background: var(--brand-accent);\n    color: #ffffff;");
  });
});

describe("index.css default dark chat contrast", () => {
  it("brightens transcript text without changing the muted surface mix", () => {
    expect(cssSource).toMatch(
      /html\.dark:not\(\[data-theme\]\) \.chat-conversation-content \{[\s\S]*?--assistant-foreground: #f2edf0;[\s\S]*?--muted-foreground: #aaa4a8;[\s\S]*?--muted: color-mix\(in srgb, #999397 13%, #242126\);/,
    );
  });
});

describe("index.css default light sidebar contrast", () => {
  it("uses softer primary ink while preserving the muted text tier", () => {
    expect(cssSource).toMatch(
      /:root:not\(\.dark\) \.conversations-sidebar:not\(\[data-collapsed\]\) \{[\s\S]*?--foreground: #464247;[\s\S]*?--sidebar-foreground: #464247;[\s\S]*?--muted-foreground: #6e6c68;[\s\S]*?color: var\(--foreground\);/,
    );
  });
});

describe("index.css landing atmosphere scope", () => {
  it("uses explicit route state instead of descendant detection", () => {
    expect(cssSource).toContain('.app-shell[data-landing="true"]::before');
    expect(cssSource).not.toContain(':has([data-testid="new-chat-landing"])');
  });

  it("keeps the landing canvas flat", () => {
    expect(cssSource).toMatch(
      /\.app-shell\[data-landing="true"\]::before \{[\s\S]*?background: none;/,
    );
    expect(cssSource).not.toContain("radial-gradient(circle at 66% 18%");
  });

  it("uses only a restrained local halo behind Otto", () => {
    expect(cssSource).toContain(".otto-landing-halo {");
    expect(cssSource).toContain("color-mix(in srgb, var(--otto-pink) 11%, transparent) 0%");
    expect(cssSource).toContain("color-mix(in srgb, var(--otto-pink) 3.5%, transparent) 42%");
    expect(cssSource).toContain("filter: blur(2px);");
    expect(cssSource).toContain(".dark .otto-landing-halo {");
    expect(cssSource).toContain("color-mix(in srgb, var(--otto-pink) 17%, transparent) 0%");
    expect(cssSource).toContain("filter: blur(3px);");
    expect(cssSource).toContain(".dark .otto-landing-mascot {");
    expect(cssSource).toContain(
      "drop-shadow(0 8px 16px color-mix(in srgb, var(--otto-pink) 13%, transparent))",
    );
  });
});

describe("index.css Omnigent code surfaces", () => {
  it("uses a compact shared code surface with quiet gutters and rose identity", () => {
    expect(cssSource).toContain("--code-block-bg: #f7f6f3;");
    expect(cssSource).toContain("--code-block-bg: #1b191c;");
    expect(cssSource).toContain('[data-streamdown="code-block"] {');
    expect(cssSource).toContain("border-radius: 10px;");
    expect(cssSource).toContain("background: var(--code-block-bg);");
    expect(cssSource).toContain('[data-streamdown="code-block-header"] > span::before');
    expect(cssSource).toContain("var(--brand-accent)");
    expect(cssSource).toContain('[data-streamdown="code-block-body"] code > span::before');
    expect(cssSource).toContain("color: var(--code-block-gutter);");
    expect(cssSource).toContain('[data-streamdown="code-block-actions"] button {');
    expect(cssSource).toContain("top: -6px;");
    expect(cssSource).toContain("width: 24px;");
    expect(cssSource).toContain("height: 14px;");
  });
});
