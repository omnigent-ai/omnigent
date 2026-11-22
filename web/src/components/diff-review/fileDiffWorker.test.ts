import path from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { build, type Rollup } from "vite";
import { beforeAll, describe, expect, it } from "vitest";

// Each diff worker as the app ships it: one bundle, loaded where there is no document.
const WORKERS = ["fileDiffWorker.ts", "markdownDiffWorker.ts"];
const chunks = new Map<string, Rollup.OutputChunk>();

async function bundle(worker: string): Promise<Rollup.OutputChunk> {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const result = await build({
    configFile: false,
    logLevel: "silent",
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

  // The Markdown worker's bundle here reaches a browser-only parser this scope lacks.
  it.runIf(worker === "fileDiffWorker.ts")("starts in a worker scope without a document", () => {
    const scope: { onmessage: unknown; postMessage: () => void } = {
      onmessage: null,
      postMessage: () => {},
    };
    // What a worker scope has that the diff relies on; no document or window.
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
  });
});
