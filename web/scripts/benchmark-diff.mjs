// Run from the repository root: node web/scripts/benchmark-diff.mjs
// Measures Monaco's computation only; browser loading/rendering are excluded.
import assert from "node:assert/strict";
import { parseArgs } from "node:util";
import { linesDiffComputers } from "monaco-editor/esm/vs/editor/common/diff/linesDiffComputers.js";

const { values } = parseArgs({
  options: {
    "budget-ms": { type: "string", default: "1000" },
    algorithm: { type: "string", default: "advanced" },
    samples: { type: "string", default: "3" },
  },
});
const budget = Number(values["budget-ms"]);
const samples = Number(values.samples);
assert(Number.isInteger(budget) && budget > 0, "budget-ms must be a positive integer");
assert(Number.isInteger(samples) && samples > 0, "samples must be a positive integer");
assert(["advanced", "legacy"].includes(values.algorithm), "algorithm must be advanced or legacy");
const computer =
  values.algorithm === "advanced"
    ? linesDiffComputers.getDefault()
    : linesDiffComputers.getLegacy();
const lines = (count) =>
  Array.from({ length: count }, (_, i) => `export const entry${i} = "original value ${i}";`);
const large = lines(5000);
const fixtures = [
  ...[100, 500, 1000, 5000].map((count) => ({
    name: `dense-${count}`,
    before: lines(count),
    after: lines(count).map((line) => line.replace("original", "changed")),
  })),
  {
    name: "sparse-5000",
    before: large,
    after: large.map((line, i) => (i % 500 === 0 ? line.replace("original", "changed") : line)),
  },
  { name: "indent-5000", before: large, after: large.map((line) => `  ${line}`) },
  { name: "move-5000", before: large, after: [...large.slice(2500), ...large.slice(0, 2500)] },
  {
    name: "repeated-5000",
    before: large.map((line, i) => (i % 2 ? "" : line)),
    after: large.map((line, i) => (i % 2 ? "" : line.replace("original", "changed"))),
  },
  {
    name: "long-line",
    before: [large.join("")],
    after: [large.join("").replaceAll("original", "changed")],
  },
  { name: "new-5000", before: [""], after: large },
  { name: "deleted-5000", before: large, after: [""] },
];

// Warm the engine before timing. Emit JSONL so results can be archived or compared.
computer.computeDiff(["old"], ["new"], {
  ignoreTrimWhitespace: false,
  maxComputationTimeMs: budget,
  computeMoves: false,
});
for (const { name, before, after } of fixtures) {
  for (let sample = 0; sample < samples; sample++) {
    const start = performance.now();
    const diff = computer.computeDiff(before, after, {
      ignoreTrimWhitespace: false,
      maxComputationTimeMs: budget,
      computeMoves: false,
    });
    const durationMs = performance.now() - start;
    // A coarse result must still account for every changed line.
    const reconstructed = before.slice();
    for (const change of [...diff.changes].reverse()) {
      reconstructed.splice(
        change.original.startLineNumber - 1,
        change.original.length,
        ...after.slice(
          change.modified.startLineNumber - 1,
          change.modified.endLineNumberExclusive - 1,
        ),
      );
    }
    assert.deepEqual(reconstructed, after, `${name}: diff omitted changed content`);
    console.log(
      JSON.stringify({
        name,
        algorithm: values.algorithm,
        budgetMs: budget,
        sample,
        durationMs: Math.round(durationMs * 10) / 10,
        hitTimeout: diff.hitTimeout,
        hunks: diff.changes.length,
        innerChanges: diff.changes.reduce(
          (sum, change) => sum + (change.innerChanges?.length ?? 0),
          0,
        ),
        hunksWithoutInnerChanges: diff.changes.filter((change) => !change.innerChanges).length,
      }),
    );
  }
}
