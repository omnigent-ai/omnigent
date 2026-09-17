import { appendFile, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const actionDirectory = path.dirname(fileURLToPath(import.meta.url));

export async function loadManifest() {
  return JSON.parse(await readFile(path.join(actionDirectory, "manifest.json"), "utf8"));
}

export function resolveHarness({
  cacheNamespace,
  harness,
  manifest,
  platform,
  requestedVersion,
  runnerTemp,
}) {
  if (!/^[a-z][a-z0-9-]*$/.test(harness ?? "")) {
    throw new Error(`Invalid harness name: ${harness ?? "unset"}`);
  }
  if (!/^[A-Za-z0-9._-]+$/.test(cacheNamespace ?? "")) {
    throw new Error(`Invalid cache namespace: ${cacheNamespace ?? "unset"}`);
  }
  const spec = manifest[harness];
  if (!spec) {
    throw new Error(`Unsupported cached harness: ${harness}`);
  }
  const version = requestedVersion || spec.version;
  if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
    throw new Error(`Harness versions must be exact semver values: ${version}`);
  }
  if (spec.kind === "archive" && version !== spec.version) {
    throw new Error(`${harness} ${version} has no pinned checksum; expected ${spec.version}`);
  }
  if (spec.kind === "archive" && !spec.platforms?.[platform]) {
    throw new Error(`${harness} has no verified archive for ${platform}`);
  }
  const cachePath = path.join(runnerTemp, "omnigent-harness-cache", harness, version, platform);
  const installPath = path.join(runnerTemp, "omnigent-harness-clis", harness, version);
  return {
    cacheKey: `harness-cli-${cacheNamespace}-${spec.kind}-${harness}-${platform}-${version}`,
    cachePath,
    installPath,
    spec,
    version,
  };
}

async function writeOutput(name, value) {
  await appendFile(process.env.GITHUB_OUTPUT, `${name}=${value}\n`);
}

async function main() {
  const manifest = await loadManifest();
  const resolved = resolveHarness({
    cacheNamespace: process.env.HARNESS_CACHE_NAMESPACE,
    harness: process.env.HARNESS,
    manifest,
    platform: `${process.env.RUNNER_OS}-${process.env.RUNNER_ARCH}`,
    requestedVersion: process.env.HARNESS_VERSION,
    runnerTemp: process.env.RUNNER_TEMP,
  });
  await writeOutput("cache-key", resolved.cacheKey);
  await writeOutput("cache-path", resolved.cachePath);
  await writeOutput("install-path", resolved.installPath);
  await writeOutput("version", resolved.version);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
