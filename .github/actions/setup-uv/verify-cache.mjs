import { createHash } from "node:crypto";
import { appendFile, lstat, readdir, readFile, rm } from "node:fs/promises";
import path from "node:path";

// Extracted from uv 0.12.15 archives verified against upstream release hashes.
const expectedByPlatform = {
  "Linux-X64": {
    uv: "5d59bc45431db192c0a49a01c517041c3bd8778adb3fbc5195aaa025eec19e23",
    uvx: "c9faf31836f0a99906793db2192fb40fc3f3c8b5ec545a99a5ff3ce6acdc031e",
  },
  "Windows-X64": {
    "uv.exe": "b0131eb55f112aee1836951ba96abec9c897b7bedd3f2fdebb19d710cfb636cd",
    "uvw.exe": "78593a0cb43be2e31de4a573c3624c7ef62f9b3dfd3f7abc9dab5b75ec814541",
    "uvx.exe": "12fa21b9a137b045d5c093ada895e2eda8494741d658ab174bd28e082fd3200c",
  },
};

async function collectFiles(directory, files = []) {
  for (const entry of await readdir(directory)) {
    const entryPath = path.join(directory, entry);
    const stat = await lstat(entryPath);
    if (stat.isSymbolicLink()) {
      throw new Error(`Cached uv tool contains a symbolic link: ${entryPath}`);
    }
    if (stat.isDirectory()) {
      await collectFiles(entryPath, files);
    } else if (stat.isFile()) {
      files.push(entryPath);
    } else {
      throw new Error(`Cached uv tool contains an unsupported entry: ${entryPath}`);
    }
  }
  return files;
}

const cacheRoot = process.env.UV_TOOL_CACHE_ROOT;
const platform = process.env.UV_CACHE_PLATFORM;
const expected = expectedByPlatform[platform];
if (!cacheRoot || !expected) {
  throw new Error(`Unsupported uv binary cache platform: ${platform ?? "unset"}`);
}

async function authenticate() {
  const files = await collectFiles(cacheRoot);
  const binaries = new Map();
  for (const file of files) {
    const name = path.basename(file);
    if (name.endsWith(".complete")) {
      continue;
    }
    if (!Object.hasOwn(expected, name)) {
      throw new Error(`Cached uv tool contains an unexpected file: ${file}`);
    }
    if (binaries.has(name)) {
      throw new Error(`Cached uv tool contains more than one ${name}`);
    }
    binaries.set(name, file);
  }

  for (const [name, expectedChecksum] of Object.entries(expected)) {
    const file = binaries.get(name);
    if (!file) {
      throw new Error(`Cached uv tool is missing ${name}`);
    }
    const actualChecksum = createHash("sha256")
      .update(await readFile(file))
      .digest("hex");
    if (actualChecksum !== expectedChecksum) {
      throw new Error(`Cached uv tool failed checksum verification: ${name}`);
    }
  }
}

try {
  await authenticate();
  if (process.env.GITHUB_OUTPUT) {
    await appendFile(process.env.GITHUB_OUTPUT, "valid=true\n");
  }
  console.log(`Authenticated cached uv binaries for ${platform}`);
} catch (error) {
  if (process.env.UV_CACHE_RECOVER_ON_FAILURE !== "true") {
    throw error;
  }
  await rm(cacheRoot, { recursive: true, force: true });
  if (process.env.GITHUB_OUTPUT) {
    await appendFile(process.env.GITHUB_OUTPUT, "valid=false\n");
  }
  console.warn(`Rejected cached uv binaries for ${platform}; installing a fresh copy`);
  console.warn(error.message);
  if (process.env.UV_CACHE_KEY && process.env.GITHUB_REPOSITORY) {
    console.warn(
      `Evict the immutable entry with: gh cache delete "${process.env.UV_CACHE_KEY}" --repo "${process.env.GITHUB_REPOSITORY}"`,
    );
  }
}
