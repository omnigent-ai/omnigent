import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const verifier = path.join(path.dirname(fileURLToPath(import.meta.url)), "verify-cache.mjs");

async function rejectedCacheFixture(filename = "payload") {
  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "setup-uv-cache-test-"));
  const cacheRoot = path.join(temporaryRoot, "uv", "0.12.15");
  const sibling = path.join(temporaryRoot, "uv", "0.12.14");
  const output = path.join(temporaryRoot, "output");
  await mkdir(cacheRoot, { recursive: true });
  await mkdir(sibling, { recursive: true });
  const rejectedFile = path.join(cacheRoot, filename);
  await writeFile(rejectedFile, "untrusted");
  await writeFile(path.join(sibling, "keep"), "sibling");
  await writeFile(output, "");
  return { cacheRoot, output, rejectedFile, sibling };
}

function runVerifier(fixture, recover) {
  return spawnSync(process.execPath, [verifier], {
    encoding: "utf8",
    env: {
      ...process.env,
      GITHUB_REPOSITORY: "omnigent-ai/omnigent",
      GITHUB_OUTPUT: fixture.output,
      UV_CACHE_KEY: "uv-tool-v3-Linux-X64-0.12.15",
      UV_CACHE_PLATFORM: "Linux-X64",
      UV_CACHE_RECOVER_ON_FAILURE: String(recover),
      UV_TOOL_CACHE_ROOT: fixture.cacheRoot,
    },
  });
}

test("rejected restored cache is removed without touching sibling versions", async () => {
  const fixture = await rejectedCacheFixture();
  const result = runVerifier(fixture, true);

  assert.equal(result.status, 0, result.stderr);
  await assert.rejects(stat(fixture.cacheRoot), { code: "ENOENT" });
  assert.equal(await readFile(path.join(fixture.sibling, "keep"), "utf8"), "sibling");
  assert.equal(await readFile(fixture.output, "utf8"), "valid=false\n");
  assert.match(
    result.stderr,
    /gh cache delete "uv-tool-v3-Linux-X64-0\.12\.15" --repo "omnigent-ai\/omnigent"/,
  );
});

test("rejected freshly installed cache remains fail-closed", async () => {
  const fixture = await rejectedCacheFixture();
  const result = runVerifier(fixture, false);

  assert.notEqual(result.status, 0);
  assert.equal(await readFile(fixture.rejectedFile, "utf8"), "untrusted");
  assert.equal(await readFile(fixture.output, "utf8"), "");
});

test("inherited object property names are rejected as unexpected files", async () => {
  const fixture = await rejectedCacheFixture("constructor");
  const result = runVerifier(fixture, false);

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /contains an unexpected file/);
});
