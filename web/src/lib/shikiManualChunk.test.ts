import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import storybookConfig from "../../.storybook/main";
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

  it("is the rule Storybook's build uses too", async () => {
    const built = (await storybookConfig.viteFinal!({}, {} as never)) as {
      build?: { rollupOptions?: { output?: { manualChunks?: unknown } } };
    };
    expect(built.build?.rollupOptions?.output?.manualChunks).toBe(shikiManualChunk);
  });

  // Vitest resolves imports as Vite does; Storybook loads its main file with plain Node,
  // whose ESM resolver needs every relative import's extension.
  it("loads Storybook's main file the way Storybook does", () => {
    const main = resolve(__dirname, "../../.storybook/main.ts");
    const loaded = execFileSync(
      process.execPath,
      [
        "--experimental-strip-types",
        "--no-warnings",
        "--input-type=module",
        "-e",
        `const { default: config } = await import(${JSON.stringify(main)}); console.log(typeof config.viteFinal);`,
      ],
      { encoding: "utf8" },
    );
    expect(loaded.trim()).toBe("function");
  });
});
