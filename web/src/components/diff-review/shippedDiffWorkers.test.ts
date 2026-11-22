import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build, createServer, type Rollup } from "vite";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { RICH_MARKDOWN_REQUEST, richMarkdownAnswer, startInWorkerScope } from "@/test/workerScope";

// The Markdown diff worker each real web build emits, the app's and the embed's, and the one
// `vite serve` gives a page.
const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const CONFIGS = ["vite.config.ts", "vite.embed.config.ts"];
const SERVED_WORKER = "/src/components/diff-review/markdownDiffWorker.ts?worker_file&type=module";
const workers = new Map<string, string>();
const scratch = mkdtempSync(path.join(tmpdir(), "shipped-diff-workers-"));
const proxyLog: unknown[][] = [];

// The dev proxy settings a developer's shell may carry: a token, which the app config reads
// while serving, and a local server, so serving never asks a workspace for one.
const PROXY_ENV = { OMNIGENT_AUTH_TOKEN: "test-token", OMNIGENT_URL: "http://localhost:6767" };

async function emittedMarkdownWorker(configFile: string): Promise<string> {
  const result = await build({
    configFile: path.join(WEB, configFile),
    root: WEB,
    logLevel: "silent",
    build: { write: false, outDir: scratch, emptyOutDir: false, sourcemap: false },
  });
  const outputs = (Array.isArray(result) ? result : [result]) as Rollup.RollupOutput[];
  const worker = outputs
    .flatMap((output) => output.output)
    .find((item) => /markdownDiffWorker-[^/]*\.js$/.test(item.fileName));
  if (!worker) throw new Error(`${configFile} emitted no Markdown diff worker`);
  if (worker.type === "chunk") return worker.code;
  return typeof worker.source === "string"
    ? worker.source
    : Buffer.from(worker.source).toString("utf8");
}

beforeAll(async () => {
  for (const [key, value] of Object.entries(PROXY_ENV)) vi.stubEnv(key, value);
  const log = vi.spyOn(console, "log").mockImplementation((...args) => void proxyLog.push(args));
  try {
    const emitted = await Promise.all(CONFIGS.map(emittedMarkdownWorker));
    CONFIGS.forEach((config, index) => workers.set(config, emitted[index]!));
  } finally {
    log.mockRestore();
  }
}, 300_000);
afterAll(() => {
  vi.unstubAllEnvs();
  rmSync(scratch, { recursive: true, force: true });
});

it("builds without the dev proxy's setup, which reads credentials and can stop the process", () => {
  expect(proxyLog.flat().filter((line) => String(line).includes("[dev-proxy]"))).toEqual([]);
});

describe.each(CONFIGS)("the Markdown diff worker %s emits", (config) => {
  it("starts where there is no DOM and renders a rich diff", async () => {
    const started = startInWorkerScope(workers.get(config)!);
    const answer = richMarkdownAnswer(await started.send(RICH_MARKDOWN_REQUEST));
    expect([answer.id, answer.status]).toEqual([1, "ready"]);
    expect(answer.rendered).toContain("katex");
    expect(answer.text).toContain("Price & fees");
  });
});

describe("the Markdown diff worker vite serve gives a page", () => {
  it("loads no package entry that needs the DOM as it starts", async () => {
    const server = await createServer({
      configFile: path.join(WEB, "vite.config.ts"),
      root: WEB,
      logLevel: "silent",
      cacheDir: path.join(scratch, "vite-cache"),
      server: { middlewareMode: true, hmr: false, ws: false },
    });
    // Every module the worker imports, as the dev server serves each one.
    const served = new Map<string, string>();
    const visit = async (url: string): Promise<void> => {
      if (served.has(url)) return;
      const result = await server.transformRequest(url);
      served.set(url, result?.code ?? "");
      const imports = [...served.get(url)!.matchAll(/(?:from|import)\s*["'](\/[^"']+)["']/g)];
      await Promise.all(imports.map(([, imported]) => visit(imported!)));
    };
    try {
      await visit(SERVED_WORKER);
    } finally {
      await server.close();
    }
    // The browser entries of hast-util-from-html-isomorphic and decode-named-character-reference.
    const domAtStartup = /new DOMParser\(|document\.createElement\(["']i["']\)/;
    const needsDom = [...served].filter(([, code]) => domAtStartup.test(code)).map(([url]) => url);
    expect(served.size).toBeGreaterThan(10);
    expect(needsDom).toEqual([]);
  }, 120_000);
});
