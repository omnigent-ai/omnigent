// The embed build post-processes the stylesheet so every rule is scoped under
// `.omnigent-app` (see vite.embed.config.ts). These tests pin the selector
// rewriting that keeps the managed (embedded) build's theming equivalent to
// standalone, where `:root`/`html`/`body` is the theme root carrying the
// `.dark` class: root-level selectors collapse onto the scope root, and
// `.dark`-rooted theme rules must match the scope root itself (which embed.tsx
// stamps with `dark`) as well as descendants — otherwise the collapsed
// `body { background: var(--background) }` resolves light tokens on the scope
// root and paints white wherever the app shell doesn't cover (the strip the
// mobile software keyboard exposes, visible at the keyboard's corners).
import { describe, expect, it } from "vitest";
import { scopeCss } from "../vite.embed.config";

describe("embed CSS scoping", () => {
  it("collapses body rules onto the scope root", () => {
    const out = scopeCss("body { background: var(--background); }");
    expect(out).toContain(".omnigent-app {");
    expect(out).not.toContain("body");
  });

  it("collapses :root token blocks (with trailing compounds) onto the scope root", () => {
    const out = scopeCss(":root:not(.dark):not([data-theme]) { --background: #fff; }");
    expect(out).toContain(".omnigent-app:not(.dark):not([data-theme])");
  });

  it("matches .dark token blocks on the scope root itself as well as descendants", () => {
    // The dark palette lives on `.dark:not([data-theme])`. Standalone puts
    // `.dark` on <html>; the embed puts it on the scope root — so the scoped
    // rule needs the compound form, or the scope root only ever resolves the
    // light tokens and paints white in dark mode.
    const out = scopeCss(".dark:not([data-theme]) { --background: #0e1013; }");
    expect(out).toContain(".omnigent-app.dark:not([data-theme])");
    expect(out).toContain(".omnigent-app .dark:not([data-theme])");
  });

  it("emits both scope-root and descendant forms for .dark-rooted descendant rules", () => {
    const out = scopeCss(".dark .conversations-sidebar { background: black; }");
    expect(out).toContain(".omnigent-app.dark .conversations-sidebar");
    expect(out).toContain(".omnigent-app .dark .conversations-sidebar");
  });

  it("handles .dark with attribute selectors (palette variants)", () => {
    const out = scopeCss('.dark[data-theme="github"] { --background: #0d1117; }');
    expect(out).toContain('.omnigent-app.dark[data-theme="github"]');
    expect(out).toContain('.omnigent-app .dark[data-theme="github"]');
  });

  it("does not treat Tailwind's escaped dark-variant utilities as theme roots", () => {
    // `.dark\:bg-black` is a class literally NAMED `dark:bg-black`, not the
    // `.dark` theme root — it must stay a plain descendant rewrite (the scope
    // root never carries utility classes).
    const out = scopeCss(".dark\\:bg-black:is(.dark *) { background: black; }");
    expect(out).toContain(".omnigent-app .dark\\:bg-black:is(.dark *)");
    expect(out).not.toContain(".omnigent-app.dark\\:bg-black");
  });

  it("leaves already-scoped selectors untouched", () => {
    const css = ".omnigent-app .foo { color: red; }";
    expect(scopeCss(css)).toBe(css);
  });
});
