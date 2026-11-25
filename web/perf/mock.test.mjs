import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

test("mock pages history without gaps and delays API responses", { timeout: 30000 }, async () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(here, "fixtures", "items_a.json"), "utf8"));
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "perf-mock-test-"));
  const server = spawn(process.execPath, [path.join(here, "server.mjs"), "--port", "0", "--out", output, "--latency-ms", "50-60"], { stdio: ["ignore", "pipe", "inherit"] });
  try {
    const [chunk] = await once(server.stdout, "data");
    const port = String(chunk).match(/127\.0\.0\.1:(\d+)/)?.[1];
    assert.ok(port, "server did not report a port");
    const base = `http://127.0.0.1:${port}`;
    const ids = [];
    async function readPage(after = "", order = "desc") {
      const query = new URLSearchParams({ limit: "100", order });
      if (after) query.set("after", after);
      const started = performance.now();
      const response = await globalThis.fetch(`${base}/v1/sessions/conv_perf_a/items?${query}`);
      assert.ok(performance.now() - started >= 40, "REST response was not delayed");
      const page = await response.json();
      ids.push(...page.data.map((item) => item.id));
      if (page.has_more) await readPage(page.last_id, order);
    }
    await readPage();
    assert.deepEqual(ids, fixture.map((item) => item.id).reverse());
    ids.length = 0;
    await readPage("", "asc");
    assert.deepEqual(ids, fixture.map((item) => item.id));
    const started = performance.now();
    const connect = await globalThis.fetch(`${base}/v1/sessions/conv_perf_a/workspace/Example/GetCapabilities`, { method: "POST", body: "{}" });
    assert.ok(connect.ok);
    assert.ok(performance.now() - started >= 40, "Connect response was not delayed");
  } finally {
    if (server.exitCode === null && server.signalCode === null) {
      server.kill("SIGTERM");
      await once(server, "exit");
    }
    const log = path.join(output, "unmatched.log");
    if (fs.existsSync(log)) fs.unlinkSync(log);
    fs.rmdirSync(output);
  }
});
