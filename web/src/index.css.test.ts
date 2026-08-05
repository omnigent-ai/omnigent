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

/* Regression test for the "table link column collapses to ~2ch" bug.
 *
 * Streamdown styles links with `wrap-anywhere`, which also drops the
 * element's min-content width to one character. Inside its auto-layout
 * table that let a link-only column ("#3090") be squeezed to ~2ch and
 * stack one character per line. index.css narrows links in table cells
 * back to `break-word`; this pins the selector so the override keeps
 * applying to cells only, and never leaks into prose links.
 */
describe("index.css table link wrapping rule", () => {
  const rule = (cssSource.match(/[^{}]+\{[^{}]*\}/g) ?? []).find(
    (block) => block.includes('[data-streamdown="table-cell"]') && /overflow-wrap\s*:/.test(block),
  );

  // Derived lazily: a missing rule must fail the assertions below with a
  // readable message, not crash at collection time.
  const selector = (rule ?? "")
    .slice(0, rule ? rule.indexOf("{") : 0)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();

  it("has the rule this test exists to protect", () => {
    expect(rule, "the table-cell link wrapping rule is gone from index.css").toBeDefined();
    expect(rule).toMatch(/overflow-wrap\s*:\s*break-word/);
  });

  function makeLink(cellAttr: string | null): HTMLElement {
    const host = document.createElement("div");
    if (cellAttr) host.setAttribute("data-streamdown", cellAttr);
    const link = document.createElement("a");
    link.setAttribute("data-streamdown", "link");
    link.className = "wrap-anywhere";
    host.appendChild(link);
    document.body.appendChild(host);
    return link;
  }

  it.each(["table-cell", "table-header-cell"])("targets links inside a %s", (cellAttr) => {
    const link = makeLink(cellAttr);
    expect(link.matches(selector)).toBe(true);
    link.parentElement?.remove();
  });

  it("leaves links outside table cells on Streamdown's wrap-anywhere", () => {
    // Prose links must keep `anywhere` so a bare overlong URL in a
    // paragraph still breaks mid-token instead of overflowing.
    const link = makeLink(null);
    expect(link.matches(selector)).toBe(false);
    link.parentElement?.remove();
  });
});

/* Regression test for the "mobile sidebar is see-through" bug.
 *
 * Below md the sidebar is a full-screen overlay on top of the chat. The
 * per-theme `.conversations-sidebar` rules paint its canvas with the
 * `background` SHORTHAND, which resets background-color to transparent — and
 * the dark stack is entirely translucent, so the conversation showed straight
 * through. A later media-query rule restores an opaque fill under the
 * gradients. It only works if it keeps matching the theme rules' specificity
 * (they'd win the tie otherwise) and stays declared after them.
 */
describe("index.css mobile sidebar opacity", () => {
  const mobileRule = cssSource.match(
    /@media \(width < 48rem\) \{[^@]*?\.conversations-sidebar[^{]*\{[^}]*background-color[^}]*\}/,
  )?.[0];

  it("keeps an opaque fill for the mobile sidebar overlay", () => {
    expect(mobileRule, "the mobile sidebar background-color rule is gone").toBeDefined();
    expect(mobileRule).toMatch(/background-color:\s*var\(--card-solid\)/);
  });

  it("declares it after the per-theme canvas rules so it wins the cascade", () => {
    // Equal specificity — the shorthand in the theme rules would otherwise
    // keep background-color transparent.
    const light = cssSource.indexOf("html:not(.dark) .conversations-sidebar {");
    const dark = cssSource.indexOf(".dark .conversations-sidebar {");
    const mobile = cssSource.indexOf(mobileRule!);
    expect(light).toBeGreaterThan(-1);
    expect(dark).toBeGreaterThan(-1);
    expect(mobile).toBeGreaterThan(Math.max(light, dark));
    // Both themes must be covered, or one of them goes transparent again.
    expect(mobileRule).toContain("html:not(.dark) .conversations-sidebar");
    expect(mobileRule).toContain(".dark .conversations-sidebar");
  });
});
