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
        for (const item of items) {
          types.add(item.type);
          assert.ok(item.response_id, `${key}: ${item.id} has no response ID`);
          assert.ok(ids.has(item.id), `${key}: ${item.type} ${item.id} was dropped`);
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
      if (fs.existsSync(fixtures)) {
        for (const file of fs.readdirSync(fixtures)) fs.unlinkSync(path.join(fixtures, file));
        fs.rmdirSync(fixtures);
      }
      fs.rmdirSync(output);
    }
  },
);
