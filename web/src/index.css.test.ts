/// <reference types="node" />
// Node types via explicit reference: the app tsconfig is browser-only, and
// importing index.css?raw instead yields "" under vitest's CSS stubbing.
import { readFileSync } from "node:fs";
// lightningcss is the minifier @tailwindcss/vite runs during `vite build`
// (resolved from its dependency tree, so we test the version the build uses).
import { transform } from "lightningcss";
import { describe, expect, it } from "vitest";

// Relative to the vitest root (web/) — import.meta.url is not a file://
// URL inside vitest's module graph, so it can't locate the file.
const cssSource = readFileSync("src/index.css", "utf8");

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
