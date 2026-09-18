import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { runWithRetries } from "./install.mjs";
import { loadManifest, resolveHarness } from "./resolve.mjs";

const actionDirectory = path.dirname(fileURLToPath(import.meta.url));

test("npm harness defaults match the committed CI dependency pins", async () => {
  const manifest = await loadManifest();
  const ciDependencies = JSON.parse(
    await readFile(path.join(actionDirectory, "..", "..", "ci-deps", "package.json"), "utf8"),
  ).dependencies;

  for (const harness of ["claude", "codex", "pi"]) {
    const spec = manifest[harness];
    assert.equal(ciDependencies[spec.package], spec.version);
    const lock = JSON.parse(await readFile(spec.lockPath, "utf8"));
    assert.equal(lock.packages[""].dependencies[spec.package], spec.version);
    for (const [packagePath, lockedPackage] of Object.entries(lock.packages)) {
      if (!packagePath || lockedPackage.link) continue;
      assert.match(lockedPackage.resolved, /^https:\/\/registry\.npmjs\.org\//);
      assert.match(lockedPackage.integrity, /^sha512-/);
    }
  }
});

test("resolver uses isolated versioned cache and install paths", async () => {
  const resolved = resolveHarness({
    cacheNamespace: "v1",
    harness: "codex",
    manifest: await loadManifest(),
    platform: "Linux-X64",
    requestedVersion: "0.140.0-alpha.1",
    runnerTemp: "/runner/temp",
  });

  assert.equal(resolved.cacheKey, "harness-cli-v1-npm-codex-Linux-X64-0.140.0-alpha.1-unlocked");
  assert.equal(resolved.cachePath, "/runner/temp/omnigent-harness-cache/codex/0.140.0-alpha.1/Linux-X64");
  assert.equal(resolved.installPath, "/runner/temp/omnigent-harness-clis/codex/0.140.0-alpha.1");
});

test("resolver keys default npm caches by committed lock digest", async () => {
  const manifest = await loadManifest();
  const resolved = resolveHarness({
    cacheNamespace: "v1",
    harness: "codex",
    manifest,
    platform: "Linux-X64",
    requestedVersion: "",
    runnerTemp: "/runner/temp",
  });

  assert.equal(resolved.useLock, true);
  assert.equal(
    resolved.cacheKey,
    `harness-cli-v1-npm-codex-Linux-X64-0.139.0-${manifest.codex.lockDigest.slice(0, 16)}`,
  );
});

test("archive harness rejects versions without a pinned checksum", async () => {
  const manifest = await loadManifest();
  assert.throws(
    () =>
      resolveHarness({
        cacheNamespace: "v1",
        harness: "agy",
        manifest,
        platform: "Linux-X64",
        requestedVersion: "1.2.5",
        runnerTemp: "/runner/temp",
      }),
    /has no pinned checksum/,
  );
});

test("retry helper uses bounded 2-second and 5-second backoffs", async () => {
  const attempts = [];
  const delays = [];
  const result = await runWithRetries(
    async (attempt) => {
      attempts.push(attempt);
      return { status: attempt === 3 ? 0 : 1 };
    },
    async (delay) => delays.push(delay),
  );

  assert.equal(result.status, 0);
  assert.deepEqual(attempts, [1, 2, 3]);
  assert.deepEqual(delays, [2000, 5000]);
});

test("retry helper propagates exhaustion after three attempts", async () => {
  const attempts = [];
  await assert.rejects(
    runWithRetries(async (attempt) => {
      attempts.push(attempt);
      return { status: 23 };
    }, async () => {}),
    /failed after 3 attempts \(exit 23\)/,
  );
  assert.deepEqual(attempts, [1, 2, 3]);
});
