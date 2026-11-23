import path from "node:path";
import { fileURLToPath } from "node:url";
import { build, type Rollup } from "vite";
import { beforeAll, describe, expect, it } from "vitest";

import { RICH_MARKDOWN_REQUEST, richMarkdownAnswer, startInWorkerScope } from "@/test/workerScope";

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

  it("starts in a worker scope without a document", () => {
    expect(() => startInWorkerScope(chunks.get(worker)!.code)).not.toThrow();
  });

  it.runIf(worker === "fileDiffWorker.ts")("answers a file diff request", async () => {
    const started = startInWorkerScope(chunks.get(worker)!.code);
    const request = { path: "src/app.ts", before: "a\n", after: "a\nb\n", ignoreWhitespace: false };
    const [answer] = (await started.send({ id: 7, request: { ...request, revision: "r1" } })) as [
      { id: number; fileDiff?: { name: string; type: string }; error?: string },
    ];
    expect(answer.error).toBeUndefined();
    expect(answer.id).toBe(7);
    expect(answer.fileDiff).toMatchObject({ name: "src/app.ts", type: "change" });
  });

  it.runIf(worker === "markdownDiffWorker.ts")("answers a rich Markdown request", async () => {
    const started = startInWorkerScope(chunks.get(worker)!.code);
    const answer = richMarkdownAnswer(await started.send(RICH_MARKDOWN_REQUEST));
    expect([answer.id, answer.status]).toEqual([1, "ready"]);
    expect(answer.rendered).toContain("katex");
    expect(answer.text).toContain("Price & fees");
  });
});
