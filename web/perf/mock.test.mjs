import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { createClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { WorkspaceService } from "../src/gen/omnigent/api/workspace/v1alpha1/workspace_pb.js";
import { cdpConnect } from "./cdp.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));

test("mock pages history without gaps and delays API responses", { timeout: 30000 }, async () => {
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "perf-mock-test-"));
  const fixtures = path.join(output, "fixtures");
  const generated = spawnSync(process.execPath, [
    path.join(here, "fixture-gen.mjs"),
    "--out",
    fixtures,
  ]);
  assert.equal(generated.status, 0, generated.stderr.toString());
  const fixture = JSON.parse(fs.readFileSync(path.join(fixtures, "items_a.json"), "utf8"));
  const server = spawn(
    process.execPath,
    [
      path.join(here, "server.mjs"),
      "--fixtures",
      fixtures,
      "--port",
      "0",
      "--out",
      output,
      "--latency-ms",
      "50-60",
    ],
    { stdio: ["ignore", "pipe", "inherit"] },
  );
  try {
    const startup = await Promise.race([
      once(server.stdout, "data").then(([chunk]) => ({ chunk })),
      once(server, "exit").then(([code]) => ({ code })),
    ]);
    assert.ok(startup.chunk, `mock server exited before startup: ${startup.code}`);
    const port = String(startup.chunk).match(/127\.0\.0\.1:(\d+)/)?.[1];
    assert.ok(port, "server did not report a port");
    const base = `http://127.0.0.1:${port}`;
    const socket = cdpConnect(`ws://127.0.0.1:${port}/v1/sessions/updates`);
    await socket.ready;
    socket.close();
    await assert.rejects(socket.send("Runtime.enable"), /CDP socket not open/);
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
    assert.deepEqual(
      ids,
      fixture.map((item) => item.id),
    );
    const unknown = await globalThis
      .fetch(`${base}/v1/sessions/conv_perf_a/items?after=missing`)
      .then((response) => response.json());
    assert.deepEqual(unknown.data, []);
    assert.equal(unknown.has_more, false);
    const zero = await globalThis
      .fetch(`${base}/v1/sessions/conv_perf_a/items?limit=0`)
      .then((response) => response.json());
    assert.equal(zero.data.length, 1);
    assert.equal(zero.has_more, true);
    const invalid = await globalThis.fetch(`${base}/__perf__/stream/start`, {
      method: "POST",
      body: "{",
    });
    assert.equal(invalid.status, 400);
    const started = performance.now();
    const connect = await globalThis.fetch(
      `${base}/v1/sessions/conv_perf_a/workspace/Example/GetCapabilities`,
      { method: "POST", body: "{}" },
    );
    assert.ok(connect.ok);
    assert.ok(performance.now() - started >= 40, "Connect response was not delayed");
    const client = createClient(
      WorkspaceService,
      createConnectTransport({
        baseUrl: `${base}/v1/sessions/conv_perf_a/workspace`,
        useBinaryFormat: true,
        fetch: globalThis.fetch,
      }),
    );
    const capabilities = await client.getCapabilities({});
    assert.ok(capabilities.capabilities.includes("ReadFile"));
    const listing = await client.listDirectory({ path: "dialog_7" });
    assert.ok(listing.entries.length > 0);
    const chunks = [];
    let metadata;
    for await (const message of client.readFile({ path: "dialog_7/stepper_00002.md" })) {
      if (message.metadata) metadata = message.metadata;
      if (message.data.length) chunks.push(message.data);
    }
    const bytes = Buffer.concat(chunks);
    assert.ok(bytes.toString("utf8").includes("export async function"));
    assert.equal(Number(metadata.size), bytes.length);
    const longPath = `${"segment/".repeat(24)}file.md`;
    const longChunks = [];
    for await (const message of client.readFile({ path: longPath })) {
      if (message.data.length) longChunks.push(message.data);
    }
    assert.ok(Buffer.concat(longChunks).length > 0);
    const live = await globalThis.fetch(`${base}/v1/sessions/conv_perf_a/stream`, {
      signal: AbortSignal.timeout(5000),
    });
    const reader = live.body.getReader();
    const badRate = await globalThis.fetch(`${base}/__perf__/stream/start`, {
      method: "POST",
      body: JSON.stringify({ session: "conv_perf_a", events_per_sec: 0 }),
    });
    assert.equal(badRate.status, 400);
    const turn = await globalThis
      .fetch(`${base}/__perf__/stream/start`, {
        method: "POST",
        body: JSON.stringify({ session: "conv_perf_a" }),
      })
      .then((response) => response.json());
    assert.ok(turn.ok);
    const stopped = await globalThis.fetch(`${base}/__perf__/stream/stop`, {
      method: "POST",
      body: JSON.stringify({ token: turn.token }),
    });
    assert.equal(stopped.status, 200);
    const stoppedBody = await stopped.json();
    assert.ok(Number.isInteger(stoppedBody.emitted));
    assert.ok(stoppedBody.emitted < turn.total_events);
    const decoder = new TextDecoder();
    async function readUntilCompleted(text = "") {
      const chunk = await reader.read();
      assert.equal(chunk.done, false);
      const next = text + decoder.decode(chunk.value, { stream: true });
      if (next.includes("event: response.completed")) return next;
      return readUntilCompleted(next);
    }
    const events = await readUntilCompleted();
    assert.ok(events.includes("event: response.output_item.done"));
    const natural = await globalThis
      .fetch(`${base}/__perf__/stream/start`, {
        method: "POST",
        body: JSON.stringify({ session: "conv_perf_a", duration_ms: 100, events_per_sec: 20 }),
      })
      .then((response) => response.json());
    await delay(250);
    const finished = await globalThis
      .fetch(`${base}/__perf__/stream/stop`, {
        method: "POST",
        body: JSON.stringify({ token: natural.token }),
      })
      .then((response) => response.json());
    assert.equal(finished.emitted, 2);
    await reader.cancel();
  } finally {
    if (server.exitCode === null && server.signalCode === null) {
      server.kill("SIGTERM");
      await once(server, "exit");
    }
    const log = path.join(output, "unmatched.log");
    if (fs.existsSync(log)) fs.unlinkSync(log);
    for (const file of fs.readdirSync(fixtures)) fs.unlinkSync(path.join(fixtures, file));
    fs.rmdirSync(fixtures);
    fs.rmdirSync(output);
  }
});
