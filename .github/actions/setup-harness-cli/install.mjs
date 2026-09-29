import { createHash } from "node:crypto";
import {
  appendFile,
  chmod,
  copyFile,
  mkdir,
  readFile,
  rm,
} from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { loadManifest, resolveHarness } from "./resolve.mjs";

const retryDelays = [2000, 5000];

export async function runWithRetries(operation, sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay))) {
  let result;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    result = await operation(attempt);
    if (result.status === 0) {
      return result;
    }
    if (attempt < 3) {
      const delay = retryDelays[attempt - 1];
      console.warn(`Harness install attempt ${attempt}/3 failed; retrying in ${delay / 1000}s`);
      await sleep(delay);
    }
  }
  throw new Error(`Harness installation failed after 3 attempts (exit ${result?.status ?? "unknown"})`);
}

function run(command, args, options = {}) {
  return spawnSync(command, args, { stdio: "inherit", ...options });
}

function evictionWarning(cacheKey) {
  if (process.env.HARNESS_CACHE_HIT === "true" && process.env.GITHUB_REPOSITORY) {
    console.warn(
      `If this cache remains unusable, evict it with: gh cache delete "${cacheKey}" --repo "${process.env.GITHUB_REPOSITORY}"`,
    );
  }
}

async function installNpmHarness(resolved) {
  const { cacheKey, cachePath, installPath, spec, useLock, version } = resolved;
  await mkdir(cachePath, { recursive: true });
  let discardedRestoredCache = false;
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  await runWithRetries(async () => {
    await rm(installPath, { recursive: true, force: true });
    await mkdir(installPath, { recursive: true });
    let args;
    if (useLock) {
      await copyFile(path.join(path.dirname(spec.lockPath), "package.json"), path.join(installPath, "package.json"));
      await copyFile(spec.lockPath, path.join(installPath, "package-lock.json"));
      args = ["ci", "--ignore-scripts"];
    } else {
      console.warn(`${spec.package}@${version} has no committed integrity lock`);
      args = ["install", "--ignore-scripts", "--package-lock=false", `${spec.package}@${version}`];
    }
    const result = run(
      npm,
      [
        ...args,
        "--no-audit",
        "--no-fund",
        "--prefer-offline",
        "--fetch-retries=0",
        "--fetch-timeout=30000",
        "--cache",
        cachePath,
      ],
      { cwd: installPath, env: { ...process.env, NPM_CONFIG_REGISTRY: "https://registry.npmjs.org/" } },
    );
    if (result.status !== 0 && process.env.HARNESS_CACHE_HIT === "true" && !discardedRestoredCache) {
      discardedRestoredCache = true;
      await rm(cachePath, { recursive: true, force: true });
      await mkdir(cachePath, { recursive: true });
      evictionWarning(cacheKey);
    }
    return result;
  });

  const packageJsonPath = path.join(installPath, "node_modules", ...spec.package.split("/"), "package.json");
  const installedPackage = JSON.parse(await readFile(packageJsonPath, "utf8"));
  if (installedPackage.version !== version) {
    throw new Error(`${spec.package} installed ${installedPackage.version}; expected ${version}`);
  }
  if (spec.installScript) {
    const result = run(process.execPath, [path.join(installPath, spec.installScript)], { cwd: installPath });
    if (result.status !== 0) {
      throw new Error(`${spec.package} install script failed with exit ${result.status ?? "unknown"}`);
    }
  }
  const binDirectory = path.join(installPath, "node_modules", ".bin");
  const binary = path.join(binDirectory, process.platform === "win32" ? `${spec.binary}.cmd` : spec.binary);
  const versionResult = run(binary, ["--version"], { shell: process.platform === "win32" });
  if (versionResult.status !== 0) {
    throw new Error(`${spec.binary} --version failed with exit ${versionResult.status ?? "unknown"}`);
  }
  await appendFile(process.env.GITHUB_PATH, `${binDirectory}\n`);
}

async function sha512(file) {
  return createHash("sha512").update(await readFile(file)).digest("hex");
}

async function installArchiveHarness(resolved, platform) {
  const { cacheKey, cachePath, installPath, spec } = resolved;
  const archive = spec.platforms[platform];
  const archivePath = path.join(cachePath, "harness.tar.gz");
  await mkdir(cachePath, { recursive: true });
  let archiveValid = false;
  try {
    archiveValid = (await sha512(archivePath)) === archive.sha512;
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (!archiveValid) {
    await rm(archivePath, { force: true });
    evictionWarning(cacheKey);
    await runWithRetries(async () =>
      run("curl", ["--fail", "--location", "--silent", "--show-error", archive.url, "--output", archivePath]),
    );
  }
  if ((await sha512(archivePath)) !== archive.sha512) {
    throw new Error(`${spec.binary} archive failed SHA-512 verification`);
  }

  await rm(installPath, { recursive: true, force: true });
  const binDirectory = path.join(installPath, "bin");
  await mkdir(binDirectory, { recursive: true });
  const extractDirectory = path.join(installPath, "extract");
  await mkdir(extractDirectory);
  const extract = run("tar", ["-xzf", archivePath, "-C", extractDirectory, archive.member]);
  if (extract.status !== 0) {
    throw new Error(`${spec.binary} archive extraction failed with exit ${extract.status ?? "unknown"}`);
  }
  const binary = path.join(binDirectory, spec.binary);
  await copyFile(path.join(extractDirectory, archive.member), binary);
  await chmod(binary, 0o755);
  const versionResult = run(binary, ["--version"]);
  if (versionResult.status !== 0) {
    throw new Error(`${spec.binary} --version failed with exit ${versionResult.status ?? "unknown"}`);
  }
  await appendFile(process.env.GITHUB_PATH, `${binDirectory}\n`);
}

async function main() {
  const platform = `${process.env.RUNNER_OS}-${process.env.RUNNER_ARCH}`;
  const resolved = resolveHarness({
    cacheNamespace: process.env.HARNESS_CACHE_NAMESPACE,
    harness: process.env.HARNESS,
    manifest: await loadManifest(),
    platform,
    requestedVersion: process.env.HARNESS_VERSION,
    runnerTemp: process.env.RUNNER_TEMP,
  });
  if (resolved.spec.kind === "npm") {
    await installNpmHarness(resolved);
  } else {
    await installArchiveHarness(resolved, platform);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await main();
}
