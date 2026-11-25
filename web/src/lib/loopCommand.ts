/** Parsing for the composer's `/loop <interval> <prompt>` and `/loop stop`. */

/** The server saves timers of at least this many seconds (its `DURABLE_TIMER_MIN_SECONDS`). */
export const LOOP_MIN_SECONDS = 120;
/** The longest interval `/loop` accepts: a week, so a typo can't park a wake for months. */
export const LOOP_MAX_SECONDS = 7 * 24 * 60 * 60;

/**
 * The message every run of a loop sends, the first run and each tick alike. The
 * prefix makes the hidden timer note read as an instruction, and keeps a prompt
 * that starts with a slash from running as a command on the first run.
 */
export function loopRunMessage(prompt: string): string {
  return `Run now: ${prompt}`;
}

/** Whether the prompt reads as a slash or skill command, which a loop passes as plain text. */
export function looksLikeCommand(prompt: string): boolean {
  return /^[/$]\S/.test(prompt);
}

export const LOOP_USAGE =
  "Usage: /loop <interval> <prompt> (e.g. /loop 5m check the deploy), or /loop stop";

const INTERVAL_PATTERN = /^(\d+)([smhd])$/i;
const UNIT_SECONDS: Readonly<Record<string, number>> = { s: 1, m: 60, h: 3600, d: 86400 };

export type LoopCommand =
  | { kind: "start"; seconds: number; interval: string; prompt: string }
  | { kind: "stop" }
  | { kind: "error"; message: string };

/**
 * Seconds for an interval token such as `5m`, `2h`, `150s` or `1d`, or `null`
 * when the token is not an interval.
 */
export function parseLoopInterval(token: string): number | null {
  const match = INTERVAL_PATTERN.exec(token);
  if (match === null) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount <= 0) return null;
  return amount * UNIT_SECONDS[match[2].toLowerCase()];
}

/** `300` → `5m`, `7200` → `2h`, `604800` → `7d`, `150` → `150s`. */
export function formatLoopInterval(seconds: number): string {
  for (const [unit, size] of [
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
  ] as const) {
    if (seconds % size === 0) return `${seconds / size}${unit}`;
  }
  return `${seconds}s`;
}

/**
 * Parse the text after `/loop`: a leading interval token followed by the prompt,
 * or the word `stop`.
 */
export function parseLoopCommand(args: string): LoopCommand {
  const text = args.trim();
  if (text === "") return { kind: "error", message: LOOP_USAGE };
  const [first, ...rest] = text.split(/\s+/);
  if (first.toLowerCase() === "stop") {
    return rest.length === 0
      ? { kind: "stop" }
      : { kind: "error", message: "/loop stop does not accept arguments" };
  }
  const seconds = parseLoopInterval(first);
  if (seconds === null) {
    return { kind: "error", message: `"${first}" is not an interval. ${LOOP_USAGE}` };
  }
  const prompt = text.slice(first.length).trim();
  if (prompt === "") {
    return { kind: "error", message: `/loop needs a prompt to repeat. ${LOOP_USAGE}` };
  }
  if (seconds < LOOP_MIN_SECONDS) {
    return {
      kind: "error",
      message: `The shortest loop is ${formatLoopInterval(LOOP_MIN_SECONDS)}; ${first} is too short.`,
    };
  }
  if (seconds > LOOP_MAX_SECONDS) {
    return {
      kind: "error",
      message: `The longest loop is ${formatLoopInterval(LOOP_MAX_SECONDS)}; ${first} is too long.`,
    };
  }
  return { kind: "start", seconds, interval: first.toLowerCase(), prompt };
}
