import { describe, expect, it } from "vitest";

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
  it("swaps entries in the module graph and the pre-bundle only while serving the app", () => {
    const serving = devServerWorkerExports("serve", "development");
    expect(serving.plugins).toHaveLength(1);
    expect(serving.optimizeDeps.rolldownOptions?.plugins).toHaveLength(1);
  });

  it.each([
    ["the tests", "serve", "test"],
    ["a build", "build", "production"],
  ])("keeps the browser entries in both halves for %s", (_name, command, mode) => {
    expect(devServerWorkerExports(command, mode)).toEqual({ plugins: [], optimizeDeps: {} });
  });
});
