import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build, type Rollup } from "vite";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { RICH_MARKDOWN_REQUEST, startInWorkerScope } from "@/test/workerScope";

// The Markdown diff worker each real web build emits, the app's and the embed's.
const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const CONFIGS = ["vite.config.ts", "vite.embed.config.ts"];
const workers = new Map<string, string>();
const outDir = mkdtempSync(path.join(tmpdir(), "shipped-diff-workers-"));

async function emittedMarkdownWorker(configFile: string): Promise<string> {
  const result = await build({
    configFile: path.join(WEB, configFile),
    root: WEB,
    logLevel: "silent",
    build: { write: false, outDir, emptyOutDir: false, sourcemap: false },
  });
  const outputs = (Array.isArray(result) ? result : [result]) as Rollup.RollupOutput[];
  const worker = outputs
    .flatMap((output) => output.output)
    .find((item) => /markdownDiffWorker-[^/]*\.js$/.test(item.fileName));
  if (!worker) throw new Error(`${configFile} emitted no Markdown diff worker`);
  return worker.type === "asset" ? String(worker.source) : worker.code;
}

beforeAll(async () => {
  const emitted = await Promise.all(CONFIGS.map(emittedMarkdownWorker));
  CONFIGS.forEach((config, index) => workers.set(config, emitted[index]!));
}, 300_000);
afterAll(() => rmSync(outDir, { recursive: true, force: true }));

describe.each(CONFIGS)("the Markdown diff worker %s emits", (config) => {
  it("starts where there is no DOM and answers a rich diff", async () => {
    const started = startInWorkerScope(workers.get(config)!);
    expect(await started.send(RICH_MARKDOWN_REQUEST)).toEqual([
      { id: 1, result: expect.objectContaining({ status: "ready" }) },
    ]);
  });
});
