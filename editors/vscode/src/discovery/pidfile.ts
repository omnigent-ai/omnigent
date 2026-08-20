/**
 * Pure pidfile parsing (contract §1).
 *
 * Format: two lines — line 1 = PID (positive integer), line 2 = port (1..65535).
 * `pidAlive` is supplied as an external observation so this stays pure and
 * testable without spawning processes (see discovery/liveness.ts for the
 * runtime probe). Conformance: docs/conformance/pidfile.json.
 */

export type PidfileResult =
  | { status: "ok"; pid: number; port: number; baseUrl: string }
  | { status: "dead"; pid: number; port: number }
  | { status: "malformed"; reason: string };

const MIN_PORT = 1;
const MAX_PORT = 65535;

/** Parse raw pidfile content given an external liveness observation. */
export function parsePidfile(content: string, pidAlive: boolean): PidfileResult {
  const lines = content
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length < 2) {
    return { status: "malformed", reason: "expected two lines (pid then port)" };
  }

  const [pidRaw, portRaw] = lines;

  if (!/^-?\d+$/.test(pidRaw)) {
    return { status: "malformed", reason: "pid is not an integer" };
  }
  const pid = Number(pidRaw);
  if (pid <= 0) {
    return { status: "malformed", reason: "pid is not a positive integer" };
  }

  if (!/^-?\d+$/.test(portRaw)) {
    return { status: "malformed", reason: "port is not an integer" };
  }
  const port = Number(portRaw);
  if (port < MIN_PORT || port > MAX_PORT) {
    return { status: "malformed", reason: "port out of range" };
  }

  if (!pidAlive) {
    return { status: "dead", pid, port };
  }

  return { status: "ok", pid, port, baseUrl: `http://127.0.0.1:${port}` };
}
