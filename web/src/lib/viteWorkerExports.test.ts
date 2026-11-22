import { describe, expect, it } from "vitest";

import type { UserConfig } from "vite";

import { devServerWorkerExports, rootPackageName, workerEntryFor } from "../../vite.workerExports";

const ROOT = "/app/node_modules/.pnpm/pkg@1/node_modules/pkg";

describe("workerEntryFor", () => {
  const manifest = {
    exports: { browser: "./index.dom.js", worker: "./index.js", default: "./index.js" },
  };

  it("swaps a package's browser entry for its worker entry", () => {
    expect(workerEntryFor(manifest, ROOT, `${ROOT}/index.dom.js`)).toBe(`${ROOT}/index.js`);
  });

  it("reads conditions under the root export too", () => {
    const nested = { exports: { ".": { browser: "./lib/browser.js", worker: "./index.js" } } };
    expect(workerEntryFor(nested, ROOT, `${ROOT}/lib/browser.js`)).toBe(`${ROOT}/index.js`);
  });

  it("keeps whatever else was resolved", () => {
    expect(workerEntryFor(manifest, ROOT, `${ROOT}/index.js`)).toBeNull();
    expect(workerEntryFor({ exports: { browser: "./b.js" } }, ROOT, `${ROOT}/b.js`)).toBeNull();
    expect(workerEntryFor({ exports: "./index.js" }, ROOT, `${ROOT}/index.js`)).toBeNull();
    expect(workerEntryFor({}, ROOT, `${ROOT}/index.js`)).toBeNull();
    const same = { exports: { browser: "./index.js", worker: "./index.js" } };
    expect(workerEntryFor(same, ROOT, `${ROOT}/index.js`)).toBeNull();
  });
});

describe("rootPackageName", () => {
  it("names a package imported by its root", () => {
    expect(rootPackageName("pkg")).toBe("pkg");
    expect(rootPackageName("@scope/pkg")).toBe("@scope/pkg");
  });

  it("leaves subpaths, files and virtual modules alone", () => {
    for (const source of [
      "pkg/sub",
      "@scope/pkg/sub",
      "./local",
      "/abs/file.js",
      "\0virtual",
      "node:fs",
      "",
    ]) {
      expect(rootPackageName(source)).toBeNull();
    }
  });
});

describe("devServerWorkerExports", () => {
  it("swaps entries only while serving the app, not for a build or the tests", () => {
    const [plugin] = devServerWorkerExports.plugins();
    const apply = plugin!.apply as (
      config: UserConfig,
      env: { command: "serve" | "build"; mode: string },
    ) => boolean;
    expect(apply({}, { command: "serve", mode: "development" })).toBe(true);
    expect(apply({}, { command: "serve", mode: "test" })).toBe(false);
    expect(apply({}, { command: "build", mode: "production" })).toBe(false);
    expect(devServerWorkerExports.optimizeDeps.rolldownOptions.plugins).toHaveLength(1);
  });
});
