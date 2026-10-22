import { createHash } from "node:crypto";
import { appendFile, lstat, readdir, readFile, rm } from "node:fs/promises";
import path from "node:path";

// Extracted from uv 0.13.4 archives verified against upstream release hashes.
const expectedByPlatform = {
  "Linux-X64": {
    uv: "5b2a2df4ef04c5d778d50702d6e1d1ff22909fc883086b4ef406a4f115e3c4fb",
    uvx: "2044b4dd5a37d593c0d6ab279777dfec833f4d4e81c0aa74b05098f595a849c7",
  },
  "Windows-X64": {
    "uv.exe": "b9d9656370ee1812e53e985d8bbb81870038106df98ef34cb45bcf4e0fe0df17",
    "uvw.exe": "2bd2d2c9beeac5014a951b3f037f0dce5877d56831949b74a6c22031532c0ea7",
    "uvx.exe": "3f63d672f7d6b841e22a595ef473a023917bff3c82de2d40e4e02f9288709517",
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
