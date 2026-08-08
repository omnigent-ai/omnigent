package ai.omnigent.intellij.discovery

/**
 * Runtime PID liveness probe, separated from the pure pidfile parser so the
 * parser stays testable. Uses ProcessHandle instead of a `kill -0` subprocess;
 * the /health probe remains the authoritative confirmation that the right
 * server is reachable (a reaped zombie pid is irrelevant here).
 */
fun isPidAlive(pid: Int): Boolean {
    if (pid <= 0) return false
    return ProcessHandle.of(pid.toLong()).map { it.isAlive }.orElse(false)
}
