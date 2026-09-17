import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const verifier = path.join(path.dirname(fileURLToPath(import.meta.url)), "verify-cache.mjs");

async function rejectedCacheFixture() {
  const temporaryRoot = await mkdtemp(path.join(os.tmpdir(), "setup-uv-cache-test-"));
  const cacheRoot = path.join(temporaryRoot, "uv", "0.12.15");
  const sibling = path.join(temporaryRoot, "uv", "0.12.14");
  const output = path.join(temporaryRoot, "output");
  await mkdir(cacheRoot, { recursive: true });
  await mkdir(sibling, { recursive: true });
  await writeFile(path.join(cacheRoot, "payload"), "untrusted");
  await writeFile(path.join(sibling, "keep"), "sibling");
  await writeFile(output, "");
  return { cacheRoot, output, sibling };
}

function runVerifier(fixture, recover) {
  return spawnSync(process.execPath, [verifier], {
    encoding: "utf8",
    env: {
      ...process.env,
      GITHUB_OUTPUT: fixture.output,
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
});

test("rejected freshly installed cache remains fail-closed", async () => {
  const fixture = await rejectedCacheFixture();
  const result = runVerifier(fixture, false);

  assert.notEqual(result.status, 0);
  assert.equal(await readFile(path.join(fixture.cacheRoot, "payload"), "utf8"), "untrusted");
  assert.equal(await readFile(fixture.output, "utf8"), "");
});
