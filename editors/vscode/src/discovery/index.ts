/**
 * Local-server discovery (A3): read ~/.omnigent/local_server.pid, parse it,
 * confirm liveness, and (optionally) probe /health. The pure logic lives in
 * pidfile.ts / health.ts; this module wires the filesystem + network IO behind
 * an injectable interface so it can be exercised without touching the real
 * home directory or network.
 */
import { homedir } from "node:os";
import { join } from "node:path";
import { readFile } from "node:fs/promises";
import { parsePidfile, PidfileResult } from "./pidfile";
import { isPidAlive } from "./liveness";
import { DEFAULT_HEALTH_TIMEOUT_MS, HealthOutcome, probeHealth } from "./health";

export * from "./pidfile";
export * from "./health";
export { isPidAlive } from "./liveness";

export const PIDFILE_PATH = join(homedir(), ".omnigent", "local_server.pid");

/** Injectable IO surface so discovery is testable without real fs/net/os. */
export interface DiscoveryIO {
  readPidfile(): Promise<string | null>;
  isPidAlive(pid: number): boolean;
  probeHealth(base: string, timeoutMs: number): Promise<HealthOutcome>;
}

/** Default IO backed by the real filesystem / OS / network. */
export const defaultDiscoveryIO: DiscoveryIO = {
  async readPidfile() {
    try {
      return await readFile(PIDFILE_PATH, "utf8");
    } catch {
      return null;
    }
  },
  isPidAlive,
  probeHealth: (base, timeoutMs) => probeHealth(base, timeoutMs),
};

export type LocalDiscovery =
  | { found: false; reason: "no-pidfile" | "malformed" | "dead" }
  | { found: true; baseUrl: string; pid: number; port: number; health: HealthOutcome };

/**
 * Attempt to discover a usable local server. Returns the parsed/probed result;
 * the caller decides whether `health === 'ok'` is required (it is, per §2/§4).
 */
export async function discoverLocalServer(
  io: DiscoveryIO = defaultDiscoveryIO,
  timeoutMs: number = DEFAULT_HEALTH_TIMEOUT_MS,
): Promise<LocalDiscovery> {
  const content = await io.readPidfile();
  if (content === null) {
    return { found: false, reason: "no-pidfile" };
  }

  const parsed: PidfileResult = parsePidfile(content, false);
  // Re-parse with the real liveness observation only if structurally valid.
  if (parsed.status === "malformed") {
    return { found: false, reason: "malformed" };
  }

  const alive = io.isPidAlive(parsed.pid);
  const result = parsePidfile(content, alive);
  if (result.status !== "ok") {
    return { found: false, reason: "dead" };
  }

  const health = await io.probeHealth(result.baseUrl, timeoutMs);
  return { found: true, baseUrl: result.baseUrl, pid: result.pid, port: result.port, health };
}
