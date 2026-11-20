import { describe, expect, it } from "vitest";

import { shikiManualChunk } from "../../vite.shiki";

// The shiki chunk loads with the app, so an app module it captured would load with it too.
const appModules = Object.keys(import.meta.glob("/src/**/*.{ts,tsx}"));

describe("shikiManualChunk", () => {
  it("groups Shiki's core and engines, wherever pnpm links them", () => {
    expect(
      shikiManualChunk("/repo/web/node_modules/.pnpm/shiki@3.2.0/node_modules/shiki/dist/core.mjs"),
    ).toBe("shiki");
    expect(
      shikiManualChunk("/repo/web/node_modules/.pnpm/node_modules/@shikijs/engine-oniguruma/x.mjs"),
    ).toBe("shiki");
    expect(shikiManualChunk("C:\\repo\\web\\node_modules\\@shikijs\\core\\dist\\index.mjs")).toBe(
      "shiki",
    );
  });

  it("leaves language grammars to their own lazy chunks", () => {
    expect(
      shikiManualChunk("/repo/web/node_modules/@shikijs/langs/dist/python.mjs"),
    ).toBeUndefined();
  });

  it("never captures the app's own modules, even ones named after Shiki", () => {
    expect(appModules.some((path) => path.includes("/shiki"))).toBe(true);
    for (const path of appModules) {
      expect(shikiManualChunk(`/repo/web${path}`), path).toBeUndefined();
    }
  });
});
