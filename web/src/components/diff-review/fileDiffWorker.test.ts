import path from "node:path";
import { fileURLToPath } from "node:url";
import { build, type Rollup } from "vite";
import { beforeAll, describe, expect, it } from "vitest";

import { RICH_MARKDOWN_REQUEST, startInWorkerScope } from "@/test/workerScope";

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
    const started = startInWorkerScope(chunks.get(worker)!.code);
    if (worker !== "markdownDiffWorker.ts") return;
    expect(await started.send(RICH_MARKDOWN_REQUEST)).toEqual([
      { id: 1, result: expect.objectContaining({ status: "ready" }) },
    ]);
  });
});
