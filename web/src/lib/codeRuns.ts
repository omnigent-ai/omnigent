// Pure helpers for running chat code blocks: which blocks run, how a block is
// identified, whether the session's agent receives runs, and the start/stop
// API calls. Mirrors `omnigent/server/routes/sessions/routes_code_runs.py`.

import { authenticatedFetch } from "./identity";

export type CodeRunLanguage = "shell" | "python" | "node";
export type CodeRunStream = "stdout" | "stderr";
export type CodeRunFinishedStatus = "exited" | "stopped" | "timed_out" | "failed";

/** Output a run keeps, start and end. Mirrors Python `CODE_RUN_OUTPUT_MAX_CHARS`. */
export const CODE_RUN_OUTPUT_MAX_CHARS = 100_000;

export interface CodeRunSegment {
  stream: CodeRunStream;
  text: string;
}

/** Output segments from the wire, dropping malformed entries. */
export function readCodeRunSegments(value: unknown): CodeRunSegment[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((segment: { stream?: unknown; text?: unknown }) =>
    (segment?.stream === "stdout" || segment?.stream === "stderr") &&
    typeof segment.text === "string"
      ? [{ stream: segment.stream, text: segment.text }]
      : [],
  );
}

// SHA-256 round constants (FIPS 180-4).
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

/**
 * Hex SHA-256 of *text*'s UTF-8 bytes.
 *
 * Synchronous on purpose: `crypto.subtle` exists only in secure contexts, so a
 * server reached over plain `http://` on a LAN address would have none.
 */
export function sha256Hex(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const padded = new Uint8Array((((bytes.length + 8) >> 6) + 1) << 6);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bytes.length / 0x20000000));
  view.setUint32(padded.length - 4, bytes.length << 3);
  const hash = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  for (let block = 0; block < padded.length; block += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(block + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15]!, 7) ^ rotr(w[i - 15]!, 18) ^ (w[i - 15]! >>> 3);
      const s1 = rotr(w[i - 2]!, 17) ^ rotr(w[i - 2]!, 19) ^ (w[i - 2]! >>> 10);
      w[i] = w[i - 16]! + s0 + w[i - 7]! + s1;
    }
    let [a, b, c, d, e, f, g, h] = hash as unknown as number[];
    for (let i = 0; i < 64; i++) {
      const t1 =
        (h! +
          (rotr(e!, 6) ^ rotr(e!, 11) ^ rotr(e!, 25)) +
          ((e! & f!) ^ (~e! & g!)) +
          K[i]! +
          w[i]!) |
        0;
      const t2 =
        ((rotr(a!, 2) ^ rotr(a!, 13) ^ rotr(a!, 22)) + ((a! & b!) ^ (a! & c!) ^ (b! & c!))) | 0;
      [h, g, f, e, d, c, b, a] = [g, f, e, (d! + t1) | 0, c, b, a, (t1 + t2) | 0];
    }
    [a, b, c, d, e, f, g, h].forEach((value, i) => {
      hash[i] = hash[i]! + value!;
    });
  }
  return Array.from(hash, (word) => word.toString(16).padStart(8, "0")).join("");
}

/** A block's text as the server compares it: LF line endings, no trailing newlines. */
export function normalizeBlockText(text: string): string {
  return text.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
}

const FENCE_LANGUAGES: Record<string, CodeRunLanguage> = {
  bash: "shell",
  sh: "shell",
  shell: "shell",
  zsh: "shell",
  console: "shell",
  python: "python",
  javascript: "node",
  node: "node",
};

// Characters Python's `str.isspace()` accepts. JavaScript's `trim()` differs:
// it keeps U+001C to U+001F and U+0085 but drops U+FEFF.
const PYTHON_WHITESPACE = new Set([
  "\t",
  "\n",
  "\u000b",
  "\u000c",
  "\r",
  "\u001c",
  "\u001d",
  "\u001e",
  "\u001f",
  " ",
  "\u0085",
  "\u00a0",
  "\u1680",
  "\u2000",
  "\u2001",
  "\u2002",
  "\u2003",
  "\u2004",
  "\u2005",
  "\u2006",
  "\u2007",
  "\u2008",
  "\u2009",
  "\u200a",
  "\u2028",
  "\u2029",
  "\u202f",
  "\u205f",
  "\u3000",
]);

/** Whether Python's `line.strip()` would leave nothing. */
function isPythonBlank(line: string): boolean {
  return [...line].every((char) => PYTHON_WHITESPACE.has(char));
}

/**
 * The language a fenced block runs as, or `null` when it can't run.
 *
 * @param tag - The fence's info-string tag, e.g. `"bash"`; `""` when untagged.
 * @param code - The block's text. An untagged block runs as shell when every
 *   non-empty line starts with `$ `.
 */
export function codeRunLanguage(tag: string, code: string): CodeRunLanguage | null {
  if (tag) return FENCE_LANGUAGES[tag.toLowerCase()] ?? null;
  // As the server does: split on "\n" only, and skip lines Python's strip() empties.
  const lines = code.split("\n").filter((line) => !isPythonBlank(line));
  return lines.length > 0 && lines.every((line) => line.startsWith("$ ")) ? "shell" : null;
}

// Every line boundary Python's `str.splitlines()` recognizes; `\r\n` counts once.
const LINE_BOUNDARIES = new Set([
  "\n",
  "\r",
  "\u000b",
  "\u000c",
  "\u001c",
  "\u001d",
  "\u001e",
  "\u0085",
  "\u2028",
  "\u2029",
]);

/** Python's `str.splitlines()`: a boundary at the very end adds no empty line. */
function splitLines(text: string): string[] {
  const lines: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    if (!LINE_BOUNDARIES.has(text[i]!)) continue;
    lines.push(text.slice(start, i));
    if (text[i] === "\r" && text[i + 1] === "\n") i++;
    start = i + 1;
  }
  if (start < text.length) lines.push(text.slice(start));
  return lines;
}

/**
 * The command a run executes, as the server derives it: shell blocks lose
 * leading `$ ` / `% ` prompts, a `console` block keeps only its prompt lines,
 * and Python or Node source is unchanged.
 */
export function deriveCodeRunCommand(
  language: CodeRunLanguage,
  code: string,
  console: boolean,
): string {
  if (language !== "shell") return code;
  return splitLines(code)
    .flatMap((line) => {
      const prompt = line.startsWith("$ ") || line.startsWith("% ");
      if (prompt) return [line.slice(2)];
      return console ? [] : [line];
    })
    .join("\n");
}

/** Mirrors Python `NATIVE_HARNESSES` (`omnigent/harness_aliases.py`). */
export const NATIVE_HARNESSES: ReadonlySet<string> = new Set([
  "agy-native",
  "antigravity-native",
  "claude-native",
  "codex-native",
  "cursor-native",
  "devin-native",
  "goose-native",
  "hermes-native",
  "kimi-native",
  "kiro-native",
  "native-agy",
  "native-antigravity",
  "native-claude",
  "native-codex",
  "native-cursor",
  "native-devin",
  "native-goose",
  "native-hermes",
  "native-kimi",
  "native-kiro",
  "native-opencode",
  "native-pi",
  "native-qwen",
  "opencode-native",
  "pi-native",
  "qwen-native",
]);

/**
 * Native harnesses the server attaches run summaries to. Mirrors Python
 * `_CODE_RUN_NOTICE_NATIVE_HARNESSES` (`server/routes/_sessions/orchestration.py`).
 */
export const CODE_RUN_NOTICE_NATIVE_HARNESSES: ReadonlySet<string> = new Set([
  "claude-native",
  "codex-native",
]);

/** Whether the session's agent gets a summary of runs on its next turn. */
export function harnessReceivesCodeRuns(harness: string | null | undefined): boolean {
  return (
    !harness || !NATIVE_HARNESSES.has(harness) || CODE_RUN_NOTICE_NATIVE_HARNESSES.has(harness)
  );
}

const CONFIRMED_KEY_PREFIX = "omnigent.codeRunsConfirmed.";

/** Whether the owner already confirmed runs in this session on this device. */
export function isCodeRunConfirmed(sessionId: string): boolean {
  try {
    return localStorage.getItem(CONFIRMED_KEY_PREFIX + sessionId) === "1";
  } catch {
    return false;
  }
}

/** Remember that runs in this session start without the confirmation dialog. */
export function confirmCodeRuns(sessionId: string): void {
  try {
    localStorage.setItem(CONFIRMED_KEY_PREFIX + sessionId, "1");
  } catch {
    // Storage unavailable (private mode): the dialog simply shows again.
  }
}

/** Status words shared by the panel and the composer text. */
export function finishedStatusText(status: CodeRunFinishedStatus, exitCode: number | null): string {
  switch (status) {
    case "exited":
      return `Exited with code ${exitCode ?? "unknown"}`;
    case "stopped":
      return "Stopped";
    case "timed_out":
      return "Timed out";
    case "failed":
      return "Failed";
  }
}

const COMPOSER_OUTPUT_CHARS = 4000;

/** A finished run as a fenced block for the composer, keeping the end of long output. */
export function codeRunComposerText(run: {
  language: CodeRunLanguage;
  command: string;
  status: CodeRunFinishedStatus;
  exitCode: number | null;
  output: CodeRunSegment[];
}): string {
  let output = run.output.map((segment) => segment.text).join("");
  if (output.length > COMPOSER_OUTPUT_CHARS) {
    output = "(output trimmed)\n" + output.slice(-COMPOSER_OUTPUT_CHARS);
  }
  const body = [
    `Command (${run.language}):`,
    run.command.replace(/\n+$/, ""),
    "",
    "Output:",
    output.replace(/\n+$/, ""),
    "",
    finishedStatusText(run.status, run.exitCode),
  ].join("\n");
  const longestRun = Math.max(0, ...(body.match(/`+/g) ?? []).map((ticks) => ticks.length));
  const fence = "`".repeat(Math.max(3, longestRun + 1));
  return `${fence}text\n${body}\n${fence}`;
}

export type StartCodeRunResult =
  { ok: true; runId: string } | { ok: false; code: string; reason: string };

/** Start a run of a block from an assistant message; refusals come back as values. */
export async function startCodeRun(
  sessionId: string,
  run: {
    messageId: string;
    blockHash: string;
    language: CodeRunLanguage;
    code: string;
    approved?: boolean;
  },
): Promise<StartCodeRunResult> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/code-runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message_id: run.messageId,
      block_hash: run.blockHash,
      language: run.language,
      code: run.code,
      approved: run.approved ?? false,
    }),
  });
  const body = (await res.json().catch(() => ({}))) as {
    run_id?: string;
    code?: string;
    reason?: string;
    detail?: string;
    error?: { code?: string; message?: string };
  };
  if (res.ok && body.run_id) return { ok: true, runId: body.run_id };
  return {
    ok: false,
    code: body.code ?? body.error?.code ?? "error",
    reason: body.reason || body.error?.message || body.detail || `Request failed (${res.status})`,
  };
}

/** Stop a running run; a finished or unknown run is a no-op on the server. */
export async function stopCodeRun(sessionId: string, runId: string): Promise<void> {
  await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/code-runs/${encodeURIComponent(runId)}/stop`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
  );
}
