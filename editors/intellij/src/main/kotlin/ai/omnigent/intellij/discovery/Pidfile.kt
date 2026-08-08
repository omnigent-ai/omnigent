package ai.omnigent.intellij.discovery

/**
 * Pure pidfile parsing.
 *
 * Format: two lines — line 1 = PID (positive integer), line 2 = port (1..65535).
 * `pidAlive` is supplied as an external observation so this stays pure and
 * testable without spawning processes (see Liveness.kt for the runtime probe).
 */
sealed class PidfileResult {
    data class Ok(val pid: Int, val port: Int, val baseUrl: String) : PidfileResult()
    data class Dead(val pid: Int, val port: Int) : PidfileResult()
    data class Malformed(val reason: String) : PidfileResult()
}

private const val MIN_PORT = 1
private const val MAX_PORT = 65535
private val INTEGER_RE = Regex("^-?\\d+$")

/** Parse raw pidfile content given an external liveness observation. */
fun parsePidfile(content: String, pidAlive: Boolean): PidfileResult {
    val lines = content.split("\n").map { it.trim() }.filter { it.isNotEmpty() }

    if (lines.size < 2) {
        return PidfileResult.Malformed("expected two lines (pid then port)")
    }

    val (pidRaw, portRaw) = lines

    if (!INTEGER_RE.matches(pidRaw)) {
        return PidfileResult.Malformed("pid is not an integer")
    }
    val pid = pidRaw.toInt()
    if (pid <= 0) {
        return PidfileResult.Malformed("pid is not a positive integer")
    }

    if (!INTEGER_RE.matches(portRaw)) {
        return PidfileResult.Malformed("port is not an integer")
    }
    val port = portRaw.toInt()
    if (port < MIN_PORT || port > MAX_PORT) {
        return PidfileResult.Malformed("port out of range")
    }

    if (!pidAlive) {
        return PidfileResult.Dead(pid, port)
    }

    return PidfileResult.Ok(pid, port, "http://127.0.0.1:$port")
}
