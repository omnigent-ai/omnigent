import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const here = path.dirname(fileURLToPath(import.meta.url));

test(
  "all generated item kinds survive the app's history converter",
  { timeout: 30000 },
  async () => {
    const web = path.resolve(here, "..");
    const output = fs.mkdtempSync(path.join(os.tmpdir(), "perf-fixture-test-"));
    const fixtures = path.join(output, "fixtures");
    let vite;
    try {
      const generated = spawnSync(process.execPath, [
        path.join(here, "fixture-gen.mjs"),
        "--out",
        fixtures,
      ]);
      assert.equal(generated.status, 0, generated.stderr.toString());
      const markdown = fs.readFileSync(path.join(fixtures, "markdown.txt"), "utf8");
      assert.ok(
        Buffer.byteLength(markdown) > 256 * 1024 && Buffer.byteLength(markdown) < 1024 * 1024,
      );
      assert.equal((markdown.match(/^## /gm) ?? []).length, 120);
      assert.equal((markdown.match(/^  - /gm) ?? []).length, 240);
      assert.equal((markdown.match(/^\| --- /gm) ?? []).length, 120);
      assert.equal((markdown.match(/^```/gm) ?? []).length, 240);
      assert.ok(markdown.includes("[the guide](https://example.com/guide)"));
      assert.ok(markdown.includes("`example_value`"));
      const tree = JSON.parse(fs.readFileSync(path.join(fixtures, "tree.json"), "utf8"));
      const entries = new Map(tree.map((entry) => [entry.path, entry]));
      assert.equal(entries.size, tree.length, "tree paths must be unique");
      for (const entry of tree) {
        const segments = entry.path.split("/");
        for (let index = 1; index < segments.length; index++) {
          const ancestor = segments.slice(0, index).join("/");
          assert.equal(
            entries.get(ancestor)?.type,
            "ENTRY_TYPE_DIRECTORY",
            `missing directory ${ancestor}`,
          );
        }
      }
      vite = await createServer({
        root: web,
        configFile: path.join(web, "vite.config.ts"),
        appType: "custom",
        server: { middlewareMode: true },
        optimizeDeps: { noDiscovery: true },
        logLevel: "error",
      });
      const { itemsToBlocks } = await vite.ssrLoadModule("/src/lib/itemsToBlocks.ts");
      const types = new Set();
      for (const key of ["a", "b", "c", "d", "e"]) {
        const items = JSON.parse(fs.readFileSync(path.join(fixtures, `items_${key}.json`), "utf8"));
        const ids = new Set(itemsToBlocks(items).map((block) => block.ctx.itemId));
        const outputs = new Map(
          items
            .filter((item) => item.type === "function_call_output")
            .map((item) => [item.call_id, item]),
        );
        for (const item of items) {
          types.add(item.type);
          assert.ok(item.response_id, `${key}: ${item.id} has no response ID`);
          assert.ok(ids.has(item.id), `${key}: ${item.type} ${item.id} was dropped`);
          if (item.type === "function_call") {
            assert.equal(
              outputs.get(item.call_id)?.response_id,
              item.response_id,
              `${key}: ${item.id} output has a different response ID`,
            );
          }
        }
      }
      for (const type of [
        "message",
        "function_call",
        "function_call_output",
        "reasoning",
        "routing_decision",
        "terminal_command",
        "slash_command",
      ]) {
        assert.ok(types.has(type), `fixture is missing ${type}`);
      }
    } finally {
      if (vite) await vite.close();
      fs.rmSync(output, { recursive: true, force: true });
    }
  },
);
