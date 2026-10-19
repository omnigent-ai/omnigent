/**
 * dbcert on a session's host (Databricks-internal). Arca boxes and managed
 * Arclets hold a copy of the laptop's corp certificate, which Arca Companion
 * syncs, and a laptop host has its own; tools that call internal services
 * fail once it expires. These helpers read and renew it through the
 * session's environment shell, on any host that has one.
 */

import { authenticatedFetch } from "@/lib/identity";

/** Prints `notAfter=<date>` for the host's dbcert, or `none` without one. */
export const DBCERT_EXPIRY_COMMAND =
  'f="$HOME/.dbcert/creds.cert.pem"; if [ -r "$f" ]; then openssl x509 -enddate -noout -in "$f"; else echo none; fi';

/**
 * Starts `dbcert` on the host and returns at once, because its browser sign-in
 * outlasts the server's shell-proxy timeout. On Arca, dbcert opens the sign-in
 * on the laptop through Arca Companion. `flock` stops a second click from
 * starting a second sign-in, and `timeout` ends an abandoned one. Exits 1 at
 * once on hosts missing any of these tools (e.g. macOS), so the banner can
 * point to Arca Companion instead of waiting out the timeout.
 */
export const DBCERT_REFRESH_COMMAND =
  'for c in dbcert setsid flock timeout; do command -v "$c" >/dev/null || exit 1; done; setsid flock -n "${TMPDIR:-/tmp}/omnigent-dbcert-refresh.lock" timeout 300 dbcert --caller-name omnigent </dev/null >/dev/null 2>&1 &';

/** How long a host-side sign-in may take: the `timeout` above. */
export const HOST_DBCERT_REFRESH_MS = 300_000;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// openssl prints e.g. `notAfter=Oct 18 20:08:15 2026 GMT`, space-padding the day.
const NOT_AFTER_RE = /^notAfter=([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4}) GMT$/;

/**
 * Parse {@link DBCERT_EXPIRY_COMMAND} output.
 *
 * @returns The expiry in epoch ms, `null` when the host has no dbcert, or
 *   `undefined` for output this doesn't recognize.
 */
export function parseDbcertExpiry(stdout: string): number | null | undefined {
  const line = stdout.trim();
  if (line === "none") return null;
  const match = NOT_AFTER_RE.exec(line);
  const month = match ? MONTHS.indexOf(match[1]) : -1;
  if (!match || month < 0) return undefined;
  const [, , day, hour, minute, second, year] = match.map(Number);
  return Date.UTC(year, month, day, hour, minute, second);
}

/** Run *command* in the session's environment; its stdout, or null on any failure. */
async function runHostShell(sessionId: string, command: string): Promise<string | null> {
  try {
    const res = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(sessionId)}/resources/environments/default/shell`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command, timeout: 10 }),
      },
    );
    if (!res.ok) return null;
    const result = (await res.json()) as { stdout?: unknown; exit_code?: unknown };
    return result.exit_code === 0 && typeof result.stdout === "string" ? result.stdout : null;
  } catch {
    return null;
  }
}

/**
 * The host's dbcert expiry, as {@link parseDbcertExpiry} returns it, or
 * `undefined` when the host can't be asked (e.g. the session has no runner).
 */
export async function readHostDbcertExpiry(sessionId: string): Promise<number | null | undefined> {
  const stdout = await runHostShell(sessionId, DBCERT_EXPIRY_COMMAND);
  return stdout === null ? undefined : parseDbcertExpiry(stdout);
}

/** Start a dbcert sign-in on the host. Resolves whether it started. */
export async function startHostDbcertRefresh(sessionId: string): Promise<boolean> {
  return (await runHostShell(sessionId, DBCERT_REFRESH_COMMAND)) !== null;
}
