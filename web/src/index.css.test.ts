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
const nativeBridgeSource = readFileSync(
  "android/app/src/main/java/ai/omnigent/android/NativeBridgeScript.kt",
  "utf8",
);

// Innermost `selector { ... }` blocks with their match indices, shared by
// every rule-extraction below so the block grammar lives in one place.
const cssBlocks = [...cssSource.matchAll(/[^{}]+\{[^{}]*\}/g)];

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

/** The selector text of an extracted rule block, minus any leading comment. */
function selectorOf(rule: string): string {
  return rule
    .slice(0, rule.indexOf("{"))
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim();
}

/** Innermost `selector { ... }` blocks that declare backdrop-filter. */
function extractBackdropFilterRules(): string[] {
  // Require a `:` so blocks that merely mention backdrop-filter in a
  // comment (e.g. the dark-token block) are not treated as glass rules.
  return cssBlocks.map(([block]) => block).filter((block) => UNPREFIXED_DECL.test(block));
}

describe("index.css backdrop-filter glass rules", () => {
  const rules = extractBackdropFilterRules();

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
  const cardRule = extractBackdropFilterRules().find((rule) => rule.includes(".bg-card"))!;
  const selector = selectorOf(cardRule);

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

const workspaceAttribute = /\[\s*aria-label\s*=\s*("Workspace"|'Workspace'|Workspace)\s*\]/;
// Rules that style the rail itself — the docked-panel rule mentions the rail
// only inside :not(...) (to exempt nested panels), so strip those first.
const workspaceRules = cssBlocks.filter(([block]) =>
  workspaceAttribute.test(selectorOf(block).replace(/:not\([^)]*\)/g, "")),
);
const dockedPanelRule =
  cssBlocks.find(
    ([block]) =>
      block.includes('[data-testid="execution-logs-panel"]') &&
      block.includes("--omnigent-safe-top") &&
      block.includes(":not("),
  )?.[0] ?? "";
const workspaceSafeAreaRules = workspaceRules.filter(([block]) =>
  block.includes("--omnigent-safe-top"),
);
const workspaceSafeAreaRule = workspaceSafeAreaRules[0]?.[0] ?? "";
const workspaceSelector = selectorOf(workspaceSafeAreaRule);
const workspaceCss = workspaceRules.map(([block]) => block).join("\n");
const fullHeightPanelRule =
  cssBlocks.find(
    ([block]) => block.includes(".conversations-sidebar") && block.includes("--omnigent-safe-top"),
  )?.[0] ?? "";
const rootSafeAreaRule =
  cssBlocks.find(
    ([block]) => block.includes(":root") && block.includes("--omnigent-safe-top"),
  )?.[0] ?? "";

const nativeInsetMatch = nativeBridgeSource.match(
  /internal val insetStyles: String\s*=\s*"""([\s\S]*?)"""\.trimIndent\(\)/,
);
const nativeInsetCss = trimIndent(nativeInsetMatch?.[1] ?? "");
// Test DOMs don't evaluate media queries, so the padding assertions run on
// the flattened rules; the phone-width guard is asserted structurally below.
const nativeInsetFlatCss = nativeInsetCss.replace(/@media[^{]*\{((?:[^{}]*\{[^{}]*\})*)\}/g, "$1");

const FULL_HEIGHT_PANEL_TARGETS = [
  { className: "conversations-sidebar" },
  { testId: "execution-logs-panel" },
  { testId: "file-viewer" },
  { testId: "files-panel-drawer" },
  { testId: "terminals-panel" },
  { testId: "subagents-panel-drawer" },
  { testId: "todos-panel-drawer" },
] as const;

function trimIndent(value: string): string {
  const lines = value
    .replace(/^\n/, "")
    .replace(/\n\s*$/, "")
    .split("\n");
  const indents = lines
    .filter((line) => line.trim())
    .map((line) => line.match(/^\s*/)?.[0].length ?? 0);
  const indent = Math.min(...indents);
  return lines.map((line) => line.slice(indent)).join("\n");
}

/* The Workspace rules key on `aria-label="Workspace"`, so a stub <aside>
 * carries the selector⇄DOM contract; the source assertion in the suite below
 * pins the real component to the same label without coupling a CSS test to
 * the component's full prop surface and data hooks. */
function renderWorkspace(platform: "android" | "ios"): HTMLElement {
  const shell = document.createElement("div");
  shell.setAttribute(`data-${platform}-native`, "");
  const aside = document.createElement("aside");
  aside.setAttribute("aria-label", "Workspace");
  shell.appendChild(aside);
  document.body.appendChild(shell);
  return aside;
}

/** Runs `assertions` with `css` applied to the document, then removes it. */
function withStyle(css: string, assertions: () => void): void {
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);
  try {
    assertions();
  } finally {
    style.remove();
  }
}

/** The four safe-area vars must land on the element's padding, edge for edge. */
function expectSafeAreaPadding(element: HTMLElement, variableFallback: string): void {
  const computed = getComputedStyle(element);
  expect(computed.paddingTop).toBe(`var(--omnigent-safe-top${variableFallback})`);
  expect(computed.paddingBottom).toBe(`var(--omnigent-safe-bottom${variableFallback})`);
  expect(computed.paddingLeft).toBe(`var(--omnigent-safe-left${variableFallback})`);
  expect(computed.paddingRight).toBe(`var(--omnigent-safe-right${variableFallback})`);
}

function assertWorkspacePadding(
  css: string,
  platform: "android" | "ios",
  variableFallback = "",
): void {
  withStyle(css, () => {
    const aside = renderWorkspace(platform);
    try {
      expectSafeAreaPadding(aside, variableFallback);
    } finally {
      aside.parentElement?.remove();
    }
  });
}

function assertFullHeightPanelPadding(css: string, variableFallback = ""): void {
  withStyle(css, () => {
    const shell = document.createElement("div");
    shell.setAttribute("data-android-native", "");
    document.body.appendChild(shell);
    try {
      for (const target of FULL_HEIGHT_PANEL_TARGETS) {
        const panel = document.createElement("div");
        if ("className" in target) panel.className = target.className;
        if ("testId" in target) panel.dataset.testid = target.testId;
        shell.appendChild(panel);
        expectSafeAreaPadding(panel, variableFallback);
      }
    } finally {
      shell.remove();
    }
  });
}

// Comments and quoted strings blanked out (index-preserving), so brace
// counting can't be fooled by braces inside string or comment text.
const cssDepthSource = cssSource.replace(
  /\/\*[\s\S]*?\*\/|"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\])*'/g,
  (match) => " ".repeat(match.length),
);

/** Brace nesting depth at a source index; 0 is top level. */
function braceDepthAt(sourceIndex: number): number {
  const prefix = cssDepthSource.slice(0, sourceIndex);
  return (prefix.match(/\{/g)?.length ?? 0) - (prefix.match(/\}/g)?.length ?? 0);
}

describe("index.css native safe-area layout", () => {
  it("folds Android and browser safe areas on both lateral edges", () => {
    withStyle(rootSafeAreaRule, () => {
      const computed = getComputedStyle(document.documentElement);
      expect(computed.getPropertyValue("--omnigent-safe-left")).toContain(
        "--omnigent-android-safe-area-left",
      );
      expect(computed.getPropertyValue("--omnigent-safe-right")).toContain(
        "--omnigent-android-safe-area-right",
      );
    });
  });

  it("keeps the Workspace rule at the top level", () => {
    expect(workspaceSafeAreaRule, "the Workspace safe-area rule is gone").not.toBe("");
    expect(workspaceSafeAreaRules).toHaveLength(1);
    expect(braceDepthAt(workspaceSafeAreaRules[0]?.index ?? -1)).toBe(0);
  });

  it.each(["android", "ios"] as const)(
    "computes four-edge padding on the Workspace rail in the %s shell",
    (platform) => assertWorkspacePadding(workspaceCss, platform),
  );

  it("keeps the Workspace aria-label on the rail component", () => {
    // The stub <aside> in renderWorkspace stands in for WorkspacePanel; this
    // pins the real component to the label the CSS selector keys on.
    const source = readFileSync("src/shell/WorkspacePanel.tsx", "utf8");
    expect(source).toMatch(/<aside[\s\S]{0,600}?aria-label="Workspace"/);
  });

  it("computes four-edge padding on every full-height native panel", () => {
    assertFullHeightPanelPadding(fullHeightPanelRule);
  });

  it("fails its layout contract when a later padding shorthand wins", () => {
    expect(workspaceSelector).not.toBe("");
    expect(() =>
      assertWorkspacePadding(`${workspaceCss}\n${workspaceSelector}{padding:0}`, "android"),
    ).toThrow();
  });

  it("does not double-count app-owned bar footprints", () => {
    expect(workspaceSafeAreaRule).not.toMatch(/--omnigent-(?:inset|native)-/);
  });

  it("pads docked right-edge panels on every edge but the content side", () => {
    // At md+ these panels replace the rail in the same slot: right edge at
    // the window edge, content to their left — so no padding-left.
    expect(dockedPanelRule, "the docked-panel safe-area rule is gone").not.toBe("");
    expect(dockedPanelRule).not.toContain("padding-left");
    withStyle(dockedPanelRule, () => {
      const shell = document.createElement("div");
      shell.setAttribute("data-android-native", "");
      const panel = document.createElement("div");
      panel.dataset.testid = "execution-logs-panel";
      shell.appendChild(panel);
      document.body.appendChild(shell);
      try {
        const computed = getComputedStyle(panel);
        expect(computed.paddingTop).toBe("var(--omnigent-safe-top)");
        expect(computed.paddingBottom).toBe("var(--omnigent-safe-bottom)");
        expect(computed.paddingRight).toBe("var(--omnigent-safe-right)");
        expect(computed.paddingLeft).toBe("0");
      } finally {
        shell.remove();
      }
    });
  });

  it("exempts panels nested inside the already-padded rail from the docked rule", () => {
    withStyle(dockedPanelRule, () => {
      const shell = document.createElement("div");
      shell.setAttribute("data-android-native", "");
      const rail = document.createElement("aside");
      rail.setAttribute("aria-label", "Workspace");
      const panel = document.createElement("div");
      panel.dataset.testid = "file-viewer";
      rail.appendChild(panel);
      shell.appendChild(rail);
      document.body.appendChild(shell);
      try {
        // The rail already pads all four edges; padding the nested viewer
        // again would double the inset.
        expect(getComputedStyle(panel).paddingTop).toBe("0");
      } finally {
        shell.remove();
      }
    });
  });
});

describe("Android injected safe-area layout", () => {
  it("exposes the production inset stylesheet to the layout test", () => {
    expect(nativeInsetMatch, "NativeBridgeScript.insetStyles is gone").not.toBeNull();
    expect(nativeInsetCss).not.toBe("");
    // insetStyles is interpolated into a JS template literal at runtime;
    // these characters would break the injected script, not these tests.
    expect(nativeInsetCss).not.toContain("`");
    expect(nativeInsetCss).not.toContain("${");
  });

  it("computes four-edge padding on the Workspace rail", () => {
    assertWorkspacePadding(nativeInsetFlatCss, "android", ", 0px");
  });

  it("computes four-edge padding on every full-height panel", () => {
    assertFullHeightPanelPadding(nativeInsetFlatCss, ", 0px");
  });

  it("scopes the panel rule to phone widths but keeps the Workspace rule global", () => {
    // Mirrors index.css: the full-height panels are phone drawers, while the
    // Workspace rail only exists at md+ — an unscoped panel rule would
    // double-pad panels docked inside the already-padded rail.
    const media = nativeInsetCss.match(/^@media \(width < 48rem\)\{(.*)\}$/m);
    expect(media?.[1], "panel rule lost its phone-width media guard").toContain(
      ".conversations-sidebar",
    );
    expect(nativeInsetCss).toMatch(/^aside\[aria-label="Workspace"\]/m);
  });

  it("pads docked md+ panels on every edge but the content side", () => {
    // Mirrors the index.css docked-panel rule for pre-shell servers: right
    // edge at the window edge, content to the left, rail descendants exempt.
    const media = nativeInsetCss.match(/^@media \(width >= 48rem\)\{(.*)\}$/m);
    expect(media?.[1], "docked-panel rule lost from the injected sheet").toContain(
      "execution-logs-panel",
    );
    expect(media?.[1]).toContain(':not(aside[aria-label="Workspace"] *)');
    expect(media?.[1]).not.toContain("padding-left");
  });

  it("pads the md+ conversations sidebar on every edge but the content side", () => {
    // At md+ the sidebar is a pinned column at the left screen edge; its
    // right edge faces content, so it gets top/bottom/left insets only.
    const rules = nativeInsetCss.match(/^@media \(width >= 48rem\)\{.*\}$/gm);
    const sidebar = rules?.find((rule) => rule.includes(".conversations-sidebar"));
    expect(sidebar, "md+ sidebar rule lost from the injected sheet").toBeDefined();
    expect(sidebar).toContain("padding-left");
    expect(sidebar).not.toContain("padding-right");
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
  const rule = cssBlocks.find(
    ([block]) =>
      block.includes('[data-streamdown="table-cell"]') && /overflow-wrap\s*:/.test(block),
  )?.[0];

  // Derived lazily: a missing rule must fail the assertions below with a
  // readable message, not crash at collection time.
  const selector = selectorOf(rule ?? "");

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
    /html:not\(\.dark\) \.conversations-sidebar \{[^}]*\}/,
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
