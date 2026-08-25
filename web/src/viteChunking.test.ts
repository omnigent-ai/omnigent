/// <reference types="node" />
// Regression test for "Shiki loads before first paint".
//
// Every Shiki entry point in the app is lazy (`@streamdown/code` behind
// lazyCodePlugin, `monacoSetup` behind the lazy Monaco viewers), but the
// `manualChunks` rule that keeps Shiki's cyclic core in one chunk used to
// absorb small modules the *eager* markdown renderer also needs: hast
// serializer utilities and the build's injected transpiler / preload helpers.
// One shared module inside the Shiki chunk is enough to make the entry chunk
// import it statically, which put Shiki's ~2 MB of themes and regex engine
// (441 kB gzipped) on the first-paint critical path.
//
// These assertions pin the routing of the modules that were the bridges.

import { describe, expect, it } from "vitest";

import config from "../vite.config";

type ChunkFn = (id: string) => string | undefined | void;

function manualChunks(): ChunkFn {
  const build = (config as { build?: { rollupOptions?: { output?: { manualChunks?: unknown } } } })
    .build;
  const fn = build?.rollupOptions?.output?.manualChunks;
  expect(typeof fn).toBe("function");
  return fn as ChunkFn;
}

const NM = "/repo/web/node_modules/.pnpm";

describe("vite manualChunks", () => {
  const route = manualChunks();

  it("keeps the build's injected helpers out of the Shiki chunk", () => {
    // NUL-prefixed virtual ids, as Rolldown reports them.
    expect(route("\0@oxc-project+runtime@0.139.0/helpers/esm/typeof.js")).toBe("runtime-helpers");
    expect(route("\0@oxc-project+runtime@0.139.0/helpers/esm/defineProperty.js")).toBe(
      "runtime-helpers",
    );
    expect(route("\0vite/preload-helper.js")).toBe("runtime-helpers");
  });

  it("keeps shared hast/markdown utilities out of the Shiki chunk", () => {
    for (const pkg of [
      "property-information",
      "hast-util-to-html",
      "hast-util-whitespace",
      "stringify-entities",
      "comma-separated-tokens",
      "space-separated-tokens",
      "html-void-elements",
      "character-entities-html4",
      "character-entities-legacy",
      "zwitch",
      "ccount",
    ]) {
      expect(route(`${NM}/${pkg}@1.0.0/node_modules/${pkg}/index.js`)).toBe("markdown-shared");
    }
  });

  it("still groups Shiki's core, engines and bundle glue together", () => {
    expect(route(`${NM}/shiki@4.2.0/node_modules/shiki/dist/index.mjs`)).toBe("shiki");
    expect(route(`${NM}/@shikijs+core@4.2.0/node_modules/@shikijs/core/dist/index.mjs`)).toBe(
      "shiki",
    );
    expect(
      route(
        `${NM}/@shikijs+engine-oniguruma@4.2.0/node_modules/@shikijs/engine-oniguruma/dist/index.mjs`,
      ),
    ).toBe("shiki");
  });

  it("leaves each Shiki language grammar as its own on-demand chunk", () => {
    expect(
      route(`${NM}/@shikijs+langs@4.2.0/node_modules/@shikijs/langs/dist/typescript.mjs`),
    ).toBeUndefined();
  });
});
