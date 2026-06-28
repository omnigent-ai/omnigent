/**
 * Runtime PID liveness probe (contract §1 "Liveness / staleness").
 *
 * Separated from the pure pidfile parser so the parser stays testable. At
 * minimum we use `process.kill(pid, 0)` (signal 0), which throws ESRCH when the
 * PID does not exist and succeeds (or throws EPERM) when it does. Deeper
 * identity checks are best-effort and intentionally out of scope; the /health
 * probe is the authoritative confirmation that the right server is reachable.
 */
export function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM => process exists but we lack permission to signal it: still alive.
    if (err && typeof err === "object" && (err as NodeJS.ErrnoException).code === "EPERM") {
      return true;
    }
    return false;
  }
}
