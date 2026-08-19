/// <reference types="node" />
// Node types via explicit reference: the app tsconfig is browser-only, and
// importing index.css?raw instead yields "" under vitest's CSS stubbing.
import { readFileSync } from "node:fs";
// lightningcss is the minifier @tailwindcss/vite runs during `vite build`
// (resolved from its dependency tree, so we test the version the build uses).
import { transform } from "lightningcss";
import { describe, expect, it } from "vitest";

import { UI_FONT_SIZE_DEFAULT, UI_FONT_SIZE_MAX, UI_FONT_SIZE_MIN } from "./lib/uiFontPreferences";

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

/* Pins the table-cell `overflow-wrap: break-word` override so it applies to the cells and never leaks into prose outside a table. */
describe("index.css table cell wrapping rule", () => {
  const rule = (cssSource.match(/[^{}]+\{[^{}]*\}/g) ?? []).find(
    (block) =>
      /\[data-streamdown="table-cell"\],?\s*\n?\s*\[data-streamdown="table-header-cell"\]\s*\{/.test(
        block,
      ) && /overflow-wrap\s*:/.test(block),
  );

  const selector = (rule ?? "")
    .slice(0, rule ? rule.indexOf("{") : 0)
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();

  it("has the rule this test exists to protect", () => {
    expect(rule, "the table-cell wrapping rule is gone from index.css").toBeDefined();
    expect(rule).toMatch(/overflow-wrap\s*:\s*break-word/);
  });

  function makeCell(cellAttr: string): HTMLElement {
    const cell = document.createElement("div");
    cell.setAttribute("data-streamdown", cellAttr);
    document.body.appendChild(cell);
    return cell;
  }

  it.each(["table-cell", "table-header-cell"])("targets the %s itself", (cellAttr) => {
    const cell = makeCell(cellAttr);
    expect(cell.matches(selector)).toBe(true);
    cell.remove();
  });

  it("leaves ordinary prose (outside a table) on the inherited wrap-anywhere", () => {
    const paragraph = document.createElement("p");
    document.body.appendChild(paragraph);
    expect(paragraph.matches(selector)).toBe(false);
    paragraph.remove();
  });

  it("is declared after the link-in-cell rule, consistent with it rather than fighting it", () => {
    const linkRuleIndex = cssSource.indexOf(
      '[data-streamdown="table-cell"], [data-streamdown="table-header-cell"])\n  [data-streamdown="link"]',
    );
    const cellRuleIndex = cssSource.indexOf(rule ?? " ");
    expect(linkRuleIndex).toBeGreaterThan(-1);
    expect(cellRuleIndex).toBeGreaterThan(linkRuleIndex);
  });
});

describe("index.css sidebar canvas", () => {
  const omniLightRule = cssSource.match(
    /:root:not\(\.dark\):not\(\[data-theme\]\) \.conversations-sidebar \{[^}]*\}/,
  )?.[0];
  const omniDarkRule = cssSource.match(
    /\.dark:not\(\[data-theme\]\) \.conversations-sidebar \{[^}]*\}/,
  )?.[0];
  const paletteRule = cssSource.match(
    /:root:not\(\.dark\)\[data-theme\] \.conversations-sidebar,[^{]*\.dark\[data-theme\] \.conversations-sidebar \{[^}]*\}/,
  )?.[0];
  const lightEdgeRule = cssSource.match(
    /html:not\(\.dark\) \.conversations-sidebar(?::not\(\.is-peek\))? \{[^}]*\}/,
  )?.[0];
  const darkEdgeRule = cssSource.match(/\.dark \.conversations-sidebar \{[^}]*\}/)?.[0];

  it("uses the specified left-to-right gradient for Omnigent light only", () => {
    expect(omniLightRule).toContain("background: #fffefe");
    expect(omniLightRule).toContain(
      "background: -webkit-linear-gradient(to right, #fffefe, #fcf6fa)",
    );
    expect(omniLightRule).toContain("background: linear-gradient(to right, #fffefe, #fcf6fa)");
    expect(paletteRule).toContain("background: var(--sidebar)");
  });

  it("removes the dot-grid layer from both modes", () => {
    expect(cssSource).not.toContain("--sidebar-dot-color");
    expect(omniLightRule).not.toContain("radial-gradient");
    expect(omniDarkRule).not.toContain("radial-gradient");
  });

  it("uses the shared inset shadow with a dark-only right border", () => {
    const shadow = "inset -8px 0 12px -8px rgb(0 0 0 / 5%)";
    expect(lightEdgeRule).toContain(`box-shadow: ${shadow}`);
    expect(darkEdgeRule).toContain(`box-shadow: ${shadow}`);
    expect(lightEdgeRule).toContain("border-right: none");
    expect(darkEdgeRule).toContain("border-right: 1px solid rgb(255 255 255 / 2%)");
  });
});

/* Regression test for the "inline <code> renders at 9.75px" bug.
 *
 * code/kbd/samp/pre carry preflight's `font-size: 1em`, so they inherit their
 * parent's step. Anchoring the title ratios to the raw 13px preference instead
 * of a 16px-equivalent shrank every step ~19%, dragging the <code> under them
 * down to 9.75px.
 */
describe("index.css desktop typography ramp", () => {
  const desktopMap = cssSource.match(/@media \(width >= 48rem\) \{\s*:root \{[^}]*\}/)?.[0];

  const anchorDivisor = Number(
    desktopMap?.match(
      /--ui-ramp-anchor:\s*calc\(var\(--desktop-ui-font-size\) \* \(16 \/ (\d+)\)\)/,
    )?.[1],
  );

  it("has the ramp block this test exists to protect", () => {
    expect(desktopMap, "the desktop typography mapping is gone from index.css").toBeDefined();
  });

  it("normalizes the preference against the 16px grid the factors assume", () => {
    // The divisor must track the shipped default, or the default size stops
    // landing on the design's steps.
    expect(anchorDivisor).toBe(UI_FONT_SIZE_DEFAULT);
  });

  it.each([
    ["--text-lg", 1.125, 18],
    ["--text-xl", 1.25, 20],
    ["--text-2xl", 1.5, 24],
  ])("scales the %s title step off the anchor, not the raw preference", (token, factor, px) => {
    // Multiplying --desktop-ui-font-size directly is the bug: it is the body
    // step, not the 16px base the ratios were calibrated against.
    expect(desktopMap).toContain(`${token}: calc(var(--ui-ramp-anchor) * ${factor})`);
    // At the default preference the step must land back on the design value.
    expect((UI_FONT_SIZE_DEFAULT * (16 / anchorDivisor) * factor).toFixed(2)).toBe(px.toFixed(2));
  });

  it("keeps the body steps on the raw preference", () => {
    // text-ui/text-base ARE the body step, so they must not be re-anchored.
    expect(desktopMap).toContain("--text-ui: var(--desktop-ui-font-size)");
    expect(desktopMap).toContain("--text-base: var(--desktop-ui-font-size)");
  });
});

/* Contract for the two body text steps.
 *
 * text-sm used to be derived from --ui-ramp-anchor, which put it at 14.4px
 * against a 13px body — a caption tier LARGER than the text it captions. It
 * must come off the body step so it stays proportional at every size the
 * Appearance setting allows.
 */
describe("index.css body text tokens", () => {
  const desktopMap = cssSource.match(/@media \(width >= 48rem\) \{\s*:root \{[^}]*\}/)?.[0];
  const mobileMap = cssSource.match(
    /@media \(width < 48rem\) \{\s*\/\*[^*]*\*\/\s*:root \{[^}]*\}/,
  )?.[0];
  const CAPTION_RATIO = 0.9;
  const LINE_HEIGHT_RATIO = 1.6;

  it("derives the caption step from the body step on desktop", () => {
    expect(desktopMap).toContain(`--text-sm: calc(var(--desktop-ui-font-size) * ${CAPTION_RATIO})`);
    // The anchor is the 16px-equivalent grid for TITLE ratios only. Routing the
    // caption factor through it is the inverted-hierarchy bug.
    expect(desktopMap).not.toContain(`--text-sm: calc(var(--ui-ramp-anchor) * ${CAPTION_RATIO})`);
  });

  it("derives the caption step from the body step on mobile too", () => {
    expect(mobileMap, "the mobile typography mapping is gone from index.css").toBeDefined();
    expect(mobileMap).toContain(`--text-sm: calc(var(--mobile-ui-font-size) * ${CAPTION_RATIO})`);
    expect(mobileMap).toContain("--text-ui: var(--mobile-ui-font-size)");
  });

  it.each([UI_FONT_SIZE_MIN, UI_FONT_SIZE_DEFAULT, UI_FONT_SIZE_MAX])(
    "keeps the caption step smaller than the body step at %ipx",
    (px) => {
      expect(px * CAPTION_RATIO).toBeLessThan(px);
    },
  );

  it("aliases the retired text-xs onto the caption step", () => {
    // Left on Tailwind's 0.75rem it would be a hard 12px against the fixed
    // 16px root, silently ignoring the Appearance setting — including in
    // vendor markup (streamdown) this app does not control.
    expect(cssSource).toContain("--text-xs: var(--text-sm)");
    expect(cssSource).toContain("--text-xs--line-height: var(--text-sm--line-height)");
    // And it must NOT be remapped in the media queries, or the alias is moot.
    expect(desktopMap).not.toContain("--text-xs:");
    expect(mobileMap).not.toContain("--text-xs:");
  });

  it("gives both steps a unitless line height so the rhythm scales", () => {
    // Unitless, not px: a fixed pair would re-freeze the line box at the
    // larger settings. 1.6 puts the 13px default on ~21px.
    const themeBlock = cssSource.match(/@theme \{[^}]*--text-ui:[^}]*\}/)?.[0];
    expect(themeBlock).toContain(`--text-ui--line-height: ${LINE_HEIGHT_RATIO}`);
    expect(themeBlock).toContain(`--text-sm--line-height: ${LINE_HEIGHT_RATIO}`);
  });

  it("retires the duplicate 13px and caption tokens", () => {
    // text-13 duplicated text-ui; text-caption was a dead fixed 12px step.
    expect(cssSource).not.toContain("--text-13:");
    expect(cssSource).not.toContain("--text-caption:");
  });
});

describe("index.css shadow tokens", () => {
  const themeBlock = cssSource.match(/@theme inline \{[^}]*\}/)?.[0].replace(/\s+/g, " ");

  it.each([
    "--shadow-composer: 0 0 12px -6px rgb(var(--ui-shadow-neutral-rgb) / 0.12)",
    "--shadow-composer-focus: 0 0 20px -4px rgb(var(--ui-shadow-neutral-rgb) / 0.12)",
    "--shadow-xs: 0 2px 8px rgb(var(--ui-shadow-neutral-rgb) / 0.04)",
    "--shadow-sm: 0 4px 10px -6px rgb(var(--ui-shadow-neutral-rgb) / 0.09)",
    "--shadow-md: 0 6px 12px -6px rgb(var(--ui-shadow-neutral-rgb) / 0.12)",
    "--shadow-lg: 0 6px 20px -4px rgb(var(--ui-shadow-neutral-rgb) / 0.12)",
    "--shadow-menu: 0 8px 28px rgb(var(--ui-shadow-warm-rgb) / 0.12)",
    "--shadow-xl: 0 12px 44px rgb(var(--ui-shadow-warm-rgb) / 0.16)",
    "--shadow-card: 0 4px 16px -2px rgb(var(--ui-shadow-warm-rgb) / 0.1), 0 1px 0 rgb(var(--ui-shadow-neutral-rgb) / 0.02)",
    "--shadow-tooltip: 0 3px 6px rgb(var(--ui-shadow-neutral-rgb) / 0.05)",
  ])("defines %s", (token) => {
    expect(themeBlock).toContain(token);
  });

  it("uses the specified light colors and black dark-mode counterparts", () => {
    expect(cssSource).toMatch(
      /:root \{[^}]*--ui-shadow-neutral-rgb: 0 0 0;[^}]*--ui-shadow-warm-rgb: 60 40 10;/s,
    );
    expect(cssSource).toMatch(
      /\.dark \{[^}]*--ui-shadow-neutral-rgb: 0 0 0;[^}]*--ui-shadow-warm-rgb: 0 0 0;/s,
    );
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
    // Matching specificity — the shorthand in the theme rules would otherwise
    // keep background-color transparent.
    const light = cssSource.indexOf(":root:not(.dark):not([data-theme]) .conversations-sidebar {");
    const dark = cssSource.indexOf(".dark:not([data-theme]) .conversations-sidebar {");
    const palette = cssSource.indexOf(":root:not(.dark)[data-theme] .conversations-sidebar,");
    const mobile = cssSource.indexOf(mobileRule!);
    expect(light).toBeGreaterThan(-1);
    expect(dark).toBeGreaterThan(-1);
    expect(palette).toBeGreaterThan(-1);
    expect(mobile).toBeGreaterThan(Math.max(light, dark, palette));
    // Every palette/mode selector must be covered, or one can go transparent.
    expect(mobileRule).toContain(":root:not(.dark):not([data-theme]) .conversations-sidebar");
    expect(mobileRule).toContain(":root:not(.dark)[data-theme] .conversations-sidebar");
    expect(mobileRule).toContain(".dark:not([data-theme]) .conversations-sidebar");
    expect(mobileRule).toContain(".dark[data-theme] .conversations-sidebar");
  });
});

describe("index.css text selection colors", () => {
  const selectionRule = cssSource.match(/::selection\s*\{([^}]*)\}/)?.[1];

  it("matches the active sidebar item in every color mode", () => {
    expect(selectionRule).toContain("background: var(--sidebar-active)");
    expect(selectionRule).toContain("color: var(--sidebar-active-foreground)");
    expect(selectionRule).not.toContain("--brand-accent");
    expect(cssSource).not.toContain(".dark ::selection");
  });
});

/* On the macOS desktop shell the window's top strip carries the OS traffic
 * lights plus the Search/Settings/toggle cluster, and the cluster is owned by
 * AppShell rather than the sidebar so it holds that spot whether the sidebar is
 * open, collapsed, or peeking. Asserted at the CSS level because the whole
 * change is CSS — and because the lights are painted by macOS OUTSIDE the page,
 * so no DOM test (and no page screenshot) can see them. These values ARE the
 * alignment.
 */
describe("index.css electron-mac sidebar header", () => {
  const sidebarRule = cssSource.match(
    /\[data-electron-mac\] \.conversations-sidebar \{[^}]*\}/,
  )?.[0];
  const headerRowRule = cssSource.match(
    /\[data-electron-mac\] \.sidebar-header-row \{[^}]*\}/,
  )?.[0];
  const brandRule = cssSource.match(/\[data-electron-mac\] \.sidebar-brand \{[^}]*\}/)?.[0];
  const inSidebarActionsRule = cssSource.match(
    /\[data-electron-mac\] \.conversations-sidebar \[data-testid="sidebar-header-actions"\] \{[^}]*\}/,
  )?.[0];
  const stripActionsRule = cssSource.match(
    /\[data-electron-mac\] \.electron-sidebar-header-actions \{(?:[^{}]|\{[^{}]*\})*\}/,
  )?.[0];
  const settingsHeaderRule = cssSource.match(
    /\[data-electron-mac\] \.settings-sidebar-header \{[^}]*\}/,
  )?.[0];
  const chatHeaderToggleRule = cssSource.match(
    /\[data-electron-mac\] \.chat-header-sidebar-toggle \{[^}]*\}/,
  )?.[0];
  const peekCardRule = cssSource.match(
    /\[data-electron-mac\] \.conversations-sidebar\.is-peek \{[^}]*\}/,
  )?.[0];
  const peekHeaderRowRule = cssSource.match(
    /\[data-electron-mac\] \.conversations-sidebar\.is-peek \.sidebar-header-row \{[^}]*\}/,
  )?.[0];

  it("starts the sidebar at the window's top edge (no empty strip above it)", () => {
    // Was 2.25rem, which left the band of blank canvas this change removes.
    expect(sidebarRule).toContain("margin-top: 0");
  });

  it("drops the brand mark, which has nowhere to sit beside the lights", () => {
    expect(brandRule).toContain("display: none");
  });

  it("collapses the emptied header row instead of leaving a dead band", () => {
    // Both the wordmark and the cluster are gone from this row on mac, so a
    // 3rem row would reintroduce the empty strip this change set out to remove.
    expect(headerRowRule).toContain("height: 2.25rem");
  });

  it("hides the sidebar's own cluster in favour of the title-bar copy", () => {
    // The AppShell copy is the one that renders on mac; two visible clusters
    // would be a duplicated control.
    expect(inSidebarActionsRule).toContain("display: none");
  });

  it("scopes that hide to the sidebar so it cannot match the AppShell copy", () => {
    // Without the .conversations-sidebar qualifier this selector would also hit
    // the title-bar cluster and hide the icons entirely on mac.
    expect(inSidebarActionsRule).toContain(".conversations-sidebar");
  });

  it("pins the title-bar cluster beside the lights, independent of the sidebar", () => {
    // Positioned against the app shell, NOT inside the sidebar — that is what
    // keeps the icons in place while the sidebar collapses (md:w-0 +
    // overflow-hidden + inert) or peeks (floating card at inset-2).
    expect(stripActionsRule).toContain("position: absolute");
  });

  it("stacks the cluster above the sidebar so it is actually painted", () => {
    // Regression guard: the sidebar is a positioned sibling at z-index 50 with
    // an OPAQUE gradient background, so any lower layer leaves the buttons
    // measuring correctly in the DOM while being invisible on screen — a bug no
    // geometry assertion catches. Must clear 50.
    const z = stripActionsRule?.match(/z-index:\s*(\d+)/)?.[1];
    expect(z, "cluster needs an explicit z-index").toBeDefined();
    expect(Number(z)).toBeGreaterThan(50);
  });

  it("hides the chat header's duplicate open-sidebar button", () => {
    // The title-bar toggle is present in every state and carries the same
    // dwell-to-peek, so the chat header's copy would be a second, lower, offset
    // instance of one control.
    expect(chatHeaderToggleRule).toContain("display: none");
  });

  it("floats the peek card below the title-bar controls", () => {
    // The card's own inset-2 would put its first row level with the lights and
    // the icon cluster, so it slides up UNDER the window controls. Its top edge
    // must clear the 2.25rem strip (2.75rem = strip + the same 0.5rem gap the
    // card's other edges use).
    expect(peekCardRule).toContain("top: 2.75rem");
  });

  it("drops the header row inside the peek card", () => {
    // The row reserves the title-bar strip for the lights and cluster, which
    // only applies to the docked sidebar starting at y=0. The peek card already
    // floats clear of all of it, so the row is 2.25rem of empty canvas above the
    // first entry — the content should line up against the card's own padding.
    expect(peekHeaderRowRule).toContain("display: none");
  });

  it("orders the cluster Collapse, Search, Settings left-to-right", () => {
    // The DOM order is Search → Settings → toggle (tab order follows
    // importance), so the toggle is reordered visually rather than moved.
    expect(stripActionsRule).toMatch(/&\s*>\s*\*\s*>\s*\*:last-child\s*\{[^}]*order:\s*-1/);
  });

  it("pushes the settings sidebar's Back row below the lights", () => {
    // /settings swaps the header row out entirely; without this its Back row
    // would sit underneath the window controls.
    expect(settingsHeaderRule).toContain("padding-top: 2.75rem");
  });

  it("keeps every header rule scoped to the desktop shell", () => {
    // A browser tab has no window controls to align to, so none of this may
    // apply there. Every SELECTOR mentioning these classes must carry the
    // [data-electron-mac] scope somewhere ahead of the class — not necessarily
    // immediately before it, since some are qualified further (e.g.
    // `[data-electron-mac] .conversations-sidebar.is-peek .sidebar-header-row`).
    // Selectors are checked whole so a leaked unscoped rule still fails.
    const selectorsInSource = [...cssSource.matchAll(/(^|\})\s*([^{}]+?)\s*\{/g)].map((m) => m[2]);
    for (const cls of [
      ".sidebar-header-row",
      ".sidebar-brand",
      ".settings-sidebar-header",
      ".electron-sidebar-header-actions",
      ".chat-header-sidebar-toggle",
    ]) {
      const mentioning = selectorsInSource.filter((sel) => sel.includes(cls));
      expect(mentioning.length, `${cls} should appear in at least one rule`).toBeGreaterThan(0);
      for (const sel of mentioning) {
        expect(sel, `${cls} must always be [data-electron-mac]-scoped`).toContain(
          "[data-electron-mac]",
        );
      }
    }
  });
});

describe("index.css native conversation breadcrumb", () => {
  it("does not hide the parent-session link on iOS/Android native shells", () => {
    // Native chrome is a server switcher, not session back. A blanket
    // `.conversation-breadcrumb { display: none }` would also drop the only
    // in-header climb-out of a sub-agent (native back is off; edge-pan opens
    // the sidebar). Folder / title / sub-agent may hide; the parent link must
    // stay.
    const blanket = cssSource.match(
      /\[data-ios-native\] \.conversation-breadcrumb\s*,\s*\[data-android-native\] \.conversation-breadcrumb\s*\{[^}]*display:\s*none/,
    );
    expect(blanket).toBeNull();
    expect(cssSource).toMatch(
      /\[data-ios-native\][\s\S]*breadcrumb-parent-link[\s\S]*\[data-android-native\][\s\S]*breadcrumb-parent-link/,
    );
  });
});
