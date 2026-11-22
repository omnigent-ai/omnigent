import path from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { build, type Rollup } from "vite";
import { beforeAll, describe, expect, it } from "vitest";

import { workerBuildPlugins } from "../../../vite.workerExports";

// Each diff worker as the app ships it: one bundle built with the app's worker plugins,
// loaded where there is no document.
const WORKERS = ["fileDiffWorker.ts", "markdownDiffWorker.ts"];
const chunks = new Map<string, Rollup.OutputChunk>();

async function bundle(worker: string): Promise<Rollup.OutputChunk> {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const result = await build({
    configFile: false,
    logLevel: "silent",
    plugins: workerBuildPlugins(),
    resolve: { alias: { "@": path.resolve(here, "../..") } },
    build: {
      write: false,
      lib: { entry: path.join(here, worker), formats: ["iife"], name: "worker" },
    },
  });
  const output = Array.isArray(result) ? result[0].output : [];
  const first = output.find((item) => item.type === "chunk");
  if (first?.type !== "chunk") throw new Error(`Expected a bundled ${worker}`);
  return first;
}

beforeAll(async () => {
  const built = await Promise.all(WORKERS.map(bundle));
  WORKERS.forEach((worker, index) => chunks.set(worker, built[index]!));
}, 120_000);

describe.each(WORKERS)("%s", (worker) => {
  const moduleIds = () => chunks.get(worker)!.moduleIds.map((id) => id.split("?")[0]!);

  it("brings no stylesheet, which a worker would try to add to a document", () => {
    expect(moduleIds().filter((id) => id.endsWith(".css"))).toEqual([]);
  });

  it("loads no React and no component", () => {
    const react = /[\\/]node_modules[\\/](react|react-dom)[\\/]/;
    expect(moduleIds().filter((id) => react.test(id) || id.endsWith(".tsx"))).toEqual([]);
  });

  it("starts in a worker scope without a document, and answers a request", async () => {
    const answers: unknown[] = [];
    const scope: {
      onmessage: ((event: { data: unknown }) => void) | null;
      postMessage: (message: unknown) => void;
    } = {
      onmessage: null,
      postMessage: (message) => answers.push(message),
    };
    // What a worker scope has that the diff relies on; no document, window or DOMParser.
    const { TextDecoder, TextEncoder, URL, queueMicrotask, setTimeout, clearTimeout } = globalThis;
    runInNewContext(chunks.get(worker)!.code, {
      self: scope,
      console,
      TextDecoder,
      TextEncoder,
      URL,
      queueMicrotask,
      setTimeout,
      clearTimeout,
    });
    expect(typeof scope.onmessage).toBe("function");
    if (worker !== "markdownDiffWorker.ts") return;
    // Entities and math reach the parts whose browser builds need the DOM.
    const before = "# Notes\n\nPrice &amp; tax\n\nInline $x^2$\n";
    scope.onmessage!({ data: { id: 1, request: { before, after: before.replace("x^2", "y^3") } } });
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(answers).toEqual([{ id: 1, result: expect.objectContaining({ status: "ready" }) }]);
  });
});
