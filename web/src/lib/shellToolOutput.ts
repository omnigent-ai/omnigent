import { keepAnsiColors } from "@/lib/ansi";

/**
 * How a shell tool reports its result: `sys_os_shell` returns `{stdout,
 * stderr, exit_code, timed_out, cwd}` JSON, Codex `shell` appends
 * `[exit code: N]` when the command failed, and the rest return plain text.
 */
export type ShellOutputFormat = "sys-os-json" | "codex-suffix" | "plain";

export interface ShellToolOutput {
  /** What the command printed (stdout then stderr); ANSI colors kept, other escapes dropped. */
  text: string;
  /** Exit code when the output reports one, otherwise `null`. */
  exitCode: number | null;
  /** Whether the command was killed for running past its timeout. */
  timedOut: boolean;
  /** Working directory the command ran in, when the output reports it. */
  cwd: string | null;
}

// The last `[exit code: N]` line that ends the output or is followed by a
// blank line, which is where Codex appends sandbox guidance after it.
const EXIT_CODE_LINE_RE = /(?:^|\n)\[exit code: (-?\d+)\](?=$|\n\n)/g;

/** Parse a shell tool's raw output into the text a terminal would show plus its status. */
export function parseShellToolOutput(output: string, format: ShellOutputFormat): ShellToolOutput {
  if (format === "sys-os-json") {
    const structured = parseStructuredOutput(output);
    if (structured !== null) return structured;
  }
  const text = normalizeOutputText(output);
  const match =
    format === "codex-suffix" ? Array.from(text.matchAll(EXIT_CODE_LINE_RE)).at(-1) : undefined;
  if (match === undefined) return { text, exitCode: null, timedOut: false, cwd: null };
  const before = text.slice(0, match.index).replace(/\n+$/, "");
  const after = text.slice(match.index + match[0].length);
  return {
    text: before.length === 0 ? after.replace(/^\n+/, "") : before + after,
    exitCode: Number(match[1]),
    timedOut: false,
    cwd: null,
  };
}

function parseStructuredOutput(output: string): ShellToolOutput | null {
  if (!output.trimStart().startsWith("{")) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(output);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const stdout = Reflect.get(parsed, "stdout");
  const stderr = Reflect.get(parsed, "stderr");
  const exitCode = Reflect.get(parsed, "exit_code");
  const cwd = Reflect.get(parsed, "cwd");
  if (typeof stdout !== "string" && typeof stderr !== "string") return null;
  // Each stream ends its own last line, so blank lines between them survive.
  const combined = [stdout, stderr]
    .filter((part): part is string => typeof part === "string" && part.length > 0)
    .map((part) => (part.endsWith("\n") ? part : `${part}\n`))
    .join("");
  return {
    text: normalizeOutputText(combined),
    exitCode: typeof exitCode === "number" && Number.isInteger(exitCode) ? exitCode : null,
    timedOut: Reflect.get(parsed, "timed_out") === true,
    cwd: typeof cwd === "string" && cwd.length > 0 ? cwd : null,
  };
}

function normalizeOutputText(text: string): string {
  return keepAnsiColors(text).replace(/\r\n/g, "\n").replace(/\n+$/, "");
}
