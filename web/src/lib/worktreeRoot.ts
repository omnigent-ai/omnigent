/** Server label holding the SHA-256 of a worktree session's canonical root. */
export const WORKTREE_ROOT_LABEL_KEY = "omnigent.git.worktree_root_sha256";

// prettier-ignore
const SHA256_ROUND_CONSTANTS = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotateRight(word: number, bits: number): number {
  return (word >>> bits) | (word << (32 - bits));
}

/** Hex SHA-256 of UTF-8 text; synchronous because Web Crypto is missing on plain-HTTP origins. */
export function sha256Hex(text: string): string {
  const message = new TextEncoder().encode(text);
  const paddedLength = Math.ceil((message.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(message);
  padded[message.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLength - 8, Math.floor(message.length / 0x20000000));
  view.setUint32(paddedLength - 4, (message.length * 8) >>> 0);

  // prettier-ignore
  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const schedule = new Uint32Array(64);
  for (let block = 0; block < paddedLength; block += 64) {
    for (let index = 0; index < 16; index++) schedule[index] = view.getUint32(block + index * 4);
    for (let index = 16; index < 64; index++) {
      const early = schedule[index - 15];
      const late = schedule[index - 2];
      const sigma0 = rotateRight(early, 7) ^ rotateRight(early, 18) ^ (early >>> 3);
      const sigma1 = rotateRight(late, 17) ^ rotateRight(late, 19) ^ (late >>> 10);
      schedule[index] = schedule[index - 16] + sigma0 + schedule[index - 7] + sigma1;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let index = 0; index < 64; index++) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + sum1 + choice + SHA256_ROUND_CONSTANTS[index] + schedule[index]) | 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      [h, g, f, e, d, c, b, a] = [g, f, e, (d + temp1) | 0, c, b, a, (temp1 + sum0 + majority) | 0];
    }
    [a, b, c, d, e, f, g, h].forEach((word, index) => (state[index] += word));
  }
  return Array.from(state, (word) => word.toString(16).padStart(8, "0")).join("");
}

/** A path split like Python's `PurePosixPath` or, for drive/UNC paths, `PureWindowsPath`. */
interface ParsedPath {
  windows: boolean;
  /** `"/"`, `"C:\\"`, `"\\\\server\\share\\"`, or `""` for a relative path. */
  anchor: string;
  parts: string[];
}

const WINDOWS_ANCHOR = /^(?:[\\/]{2}([^\\/]+)[\\/]+([^\\/]+)|([a-zA-Z]:)[\\/])/;

function parsePath(path: string): ParsedPath {
  const match = WINDOWS_ANCHOR.exec(path);
  if (match) {
    const anchor = match[3] ? `${match[3]}\\` : `\\\\${match[1]}\\${match[2]}\\`;
    const parts = path.slice(match[0].length).split(/[\\/]+/);
    return { windows: true, anchor, parts: parts.filter((part) => part !== "" && part !== ".") };
  }
  const parts = path.split("/").filter((part) => part !== "" && part !== ".");
  return { windows: false, anchor: path.startsWith("/") ? "/" : "", parts };
}

function formatPath({ windows, anchor, parts }: ParsedPath): string {
  return anchor + parts.join(windows ? "\\" : "/") || ".";
}

/** Server `worktree_root_fingerprint`: Windows roots ignore case and separators. */
export function worktreeRootFingerprint(path: string): string {
  const normalized = WINDOWS_ANCHOR.test(path) ? path.replaceAll("\\", "/").toLowerCase() : path;
  return sha256Hex(normalized.replace(/\/+$/, ""));
}

/** Root the server's `recorded_worktree_root` removes for a session directory, or null. */
export function recordedWorktreeRoot(
  workspace: string,
  fingerprint: string | undefined,
): string | null {
  if (fingerprint === undefined) return workspace;
  const parsed = parsePath(workspace);
  for (let depth = parsed.parts.length; depth >= 0; depth--) {
    const candidate = formatPath({ ...parsed, parts: parsed.parts.slice(0, depth) });
    if (worktreeRootFingerprint(candidate) === fingerprint) return candidate;
  }
  return null;
}

/** Last path component, e.g. `"login"`; the anchor for a root such as `"C:\\"`. */
export function worktreeDirectoryName(path: string): string {
  const parsed = parsePath(path);
  return parsed.parts.at(-1) ?? (parsed.anchor || path);
}
