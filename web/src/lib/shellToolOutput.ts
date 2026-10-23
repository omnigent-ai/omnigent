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

// ESC-introduced control sequences (colors, cursor moves, OSC titles). An OSC
// may end at the end of the text when truncation cut off its terminator.
const ANSI_ESCAPE_RE =
  // eslint-disable-next-line no-control-regex -- intentionally stripping terminal escapes
  /\u001b(?:\[[0-?]*[ -/]*[@-~]|\][^\u0007\u001b]*(?:\u0007|\u001b\\|$)|[@-Z\\-_])/g;

// Select Graphic Rendition: the color / bold / underline escapes.
// eslint-disable-next-line no-control-regex -- matching terminal escapes
const SGR_RE = /^\u001b\[[0-9;:]*m$/;

// Colon-form truecolor / 256-color (`38:2::255:0:0`, `48:5:208`), which the
// renderer only understands in the semicolon form.
const COLON_COLOR_RE = /([34]8):(?:2:(?:\d*:)?(\d+):(\d+):(\d+)|5:(\d+))/g;

// eslint-disable-next-line no-control-regex -- matching terminal escapes
const TRAILING_PARTIAL_ESCAPE_RE = /\u001b(?:\[[0-?]*[ -/]*)?$/;

/**
 * Parse a shell tool's raw output into the text a terminal would show plus
 * the exit code. `sys_os_shell` returns `{stdout, stderr, exit_code,
 * timed_out, cwd}` JSON, Codex `shell` appends `[exit code: N]` when the command
 * failed, and other harnesses (Claude `Bash`, Pi/OpenCode `bash`) return
 * plain text.
 */
export function parseShellToolOutput(output: string): ShellToolOutput {
  const structured = parseStructuredOutput(output);
  if (structured !== null) return structured;

  const text = normalizeOutputText(output);
  const match = Array.from(text.matchAll(EXIT_CODE_LINE_RE)).at(-1);
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
  const parts = [stdout, stderr]
    .filter((part): part is string => typeof part === "string")
    .map(normalizeOutputText)
    .filter((part) => part.length > 0);
  return {
    text: parts.join("\n"),
    exitCode: typeof exitCode === "number" && Number.isInteger(exitCode) ? exitCode : null,
    timedOut: Reflect.get(parsed, "timed_out") === true,
    cwd: typeof cwd === "string" && cwd.length > 0 ? cwd : null,
  };
}

function normalizeOutputText(text: string): string {
  return keepColorEscapesOnly(text).replace(/\r\n/g, "\n").replace(/\n+$/, "");
}

// Cursor moves, line clears and title updates mean nothing in a static
// transcript, and the renderer would show them as raw characters. The short
// reset `ESC[m` (used by git) is spelled out because the renderer ignores it.
function keepColorEscapesOnly(text: string): string {
  return text.replace(ANSI_ESCAPE_RE, (sequence) => {
    if (!SGR_RE.test(sequence)) return "";
    if (sequence === "\u001b[m") return "\u001b[0m";
    return sequence.replace(COLON_COLOR_RE, (_all, target, red, green, blue, index) =>
      index === undefined ? `${target};2;${red};${green};${blue}` : `${target};5;${index}`,
    );
  });
}

/** Remove ANSI escape sequences, e.g. for copying output as plain text. */
export function stripAnsi(text: string): string {
  return text.replace(ANSI_ESCAPE_RE, "");
}

/** Drop an escape sequence cut off at the end of the text, e.g. by a preview limit. */
export function dropTrailingPartialEscape(text: string): string {
  return text.replace(TRAILING_PARTIAL_ESCAPE_RE, "");
}
