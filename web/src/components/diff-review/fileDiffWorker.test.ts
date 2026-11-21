import path from "node:path";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import { build, type Rollup } from "vite";
import { beforeAll, expect, it } from "vitest";

// The worker as the app ships it: one bundle, loaded where there is no document.
let chunk: Rollup.OutputChunk;

beforeAll(async () => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const result = await build({
    configFile: false,
    logLevel: "silent",
    resolve: { alias: { "@": path.resolve(here, "../..") } },
    build: {
      write: false,
      lib: { entry: path.join(here, "fileDiffWorker.ts"), formats: ["iife"], name: "worker" },
    },
  });
  const output = Array.isArray(result) ? result[0].output : [];
  const first = output.find((item) => item.type === "chunk");
  if (first?.type !== "chunk") throw new Error("Expected a bundled diff worker");
  chunk = first;
}, 60_000);

it("brings no stylesheet, which a worker would try to add to a document", () => {
  expect(chunk.moduleIds.filter((id) => id.split("?")[0].endsWith(".css"))).toEqual([]);
});

it("starts in a worker scope without a document", () => {
  const scope: { onmessage: unknown; postMessage: () => void } = {
    onmessage: null,
    postMessage: () => {},
  };
  // What a worker scope has that the diff relies on; no document or window.
  const { TextDecoder, TextEncoder, URL, queueMicrotask, setTimeout, clearTimeout } = globalThis;
  runInNewContext(chunk.code, {
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
