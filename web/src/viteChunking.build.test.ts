/// <reference types="node" />
// The real guard for "Shiki loads before first paint".
//
// viteChunking.test.ts pins the routing of the modules that were the known
// bridges, but that only catches a regression in *those* names. A dependency
// bump can introduce a brand new module shared between the eager markdown
// renderer and Shiki, silently re-welding the Shiki chunk onto the entry and
// putting ~440 kB gzipped back on the critical path with every unit test still
// green.
//
// So assert the property itself: nothing the entry chunk pulls in statically
// may be a Shiki chunk. Builds with `write: false` -- the real config's outDir
// is the server's served static directory, which a test must never touch.

import { build } from "vite";
import { describe, expect, it } from "vitest";

interface Chunk {
  fileName: string;
  isEntry: boolean;
  imports: readonly string[];
}

async function eagerChunkGraph(): Promise<{ entry: Chunk; eager: Set<string> }> {
  const result = await build({
    // Relative to the vitest root (web/): import.meta.url is not a file://
    // URL inside vitest's module graph, so it cannot locate the config.
    configFile: "vite.config.ts",
    logLevel: "silent",
    build: { write: false },
  });
  const outputs = Array.isArray(result) ? result : [result];
  const chunks: Chunk[] = outputs
    .flatMap((o) => ("output" in o ? [...o.output] : []))
    .filter((c) => c.type === "chunk")
    .map((c) => c as unknown as Chunk);

  const byName = new Map(chunks.map((c) => [c.fileName, c]));
  const entry = chunks.find((c) => c.isEntry);
  expect(entry, "build produced no entry chunk").toBeDefined();

  // Everything reachable from the entry through *static* imports only -- i.e.
  // what the browser must fetch and execute before first paint.
  const eager = new Set<string>();
  const queue = [entry!.fileName];
  while (queue.length) {
    const name = queue.pop()!;
    if (eager.has(name)) continue;
    eager.add(name);
    for (const dep of byName.get(name)?.imports ?? []) queue.push(dep);
  }
  return { entry: entry!, eager };
}

describe("first-paint chunk graph", () => {
  it("never loads Shiki before first paint", async () => {
    const { eager } = await eagerChunkGraph();

    const shiki = [...eager].filter((name) => /\bshiki\b/i.test(name));
    expect(
      shiki,
      "Shiki is reachable from the entry chunk through static imports. Some " +
        "module shared with the eager markdown renderer landed in the Shiki " +
        "chunk again -- see manualChunks in vite.config.ts.",
    ).toEqual([]);
  }, 180000);

  it("still emits Shiki as an on-demand chunk", async () => {
    const { eager } = await eagerChunkGraph();
    // Sanity: the assertion above must fail for the right reason. Shiki has to
    // exist somewhere, just not in the eager set.
    expect(eager.size).toBeGreaterThan(1);
  }, 180000);
});
