package ai.omnigent.intellij.discovery

import java.nio.file.Files
import java.nio.file.Paths
import ai.omnigent.intellij.discovery.isPidAlive as osIsPidAlive
import ai.omnigent.intellij.discovery.probeHealth as httpProbeHealth

/**
 * Local-server discovery: read ~/.omnigent/local_server.pid, parse it, confirm
 * liveness, and (optionally) probe /health. The pure logic lives in Pidfile.kt
 * / Health.kt; this wires the filesystem + network IO behind an injectable
 * interface so it can be exercised without touching the real home directory
 * or network.
 */
val PIDFILE_PATH: String = Paths.get(System.getProperty("user.home"), ".omnigent", "local_server.pid").toString()

/** Injectable IO surface so discovery is testable without real fs/net. */
interface DiscoveryIO {
    fun readPidfile(): String?
    fun isPidAlive(pid: Int): Boolean
    fun probeHealth(base: String, timeoutMs: Long): HealthOutcome
}

/** Default IO backed by the real filesystem / OS / network. */
val defaultDiscoveryIO: DiscoveryIO = object : DiscoveryIO {
    override fun readPidfile(): String? {
        return try {
            Files.readString(Paths.get(PIDFILE_PATH))
        } catch (e: Exception) {
            null
        }
    }

    // Qualified aliases: an unqualified call here would bind to this member and recurse.
    override fun isPidAlive(pid: Int): Boolean = osIsPidAlive(pid)

    override fun probeHealth(base: String, timeoutMs: Long): HealthOutcome = httpProbeHealth(base, timeoutMs)
}

sealed class LocalDiscovery {
    data class NotFound(val reason: String) : LocalDiscovery()
    data class Found(val baseUrl: String, val pid: Int, val port: Int, val health: HealthOutcome) : LocalDiscovery()
}

/**
 * Attempt to discover a usable local server. Returns the parsed/probed result;
 * the caller decides whether health == OK is required.
 */
fun discoverLocalServer(
    io: DiscoveryIO = defaultDiscoveryIO,
    timeoutMs: Long = DEFAULT_HEALTH_TIMEOUT_MS,
): LocalDiscovery {
    val content = io.readPidfile() ?: return LocalDiscovery.NotFound("no-pidfile")

    val parsed = parsePidfile(content, false)
    // Re-parse with the real liveness observation only if structurally valid.
    if (parsed is PidfileResult.Malformed) {
        return LocalDiscovery.NotFound("malformed")
    }

    val pid = when (parsed) {
        is PidfileResult.Ok -> parsed.pid
        is PidfileResult.Dead -> parsed.pid
        is PidfileResult.Malformed -> error("unreachable")
    }
    val alive = io.isPidAlive(pid)
    val result = parsePidfile(content, alive)
    if (result !is PidfileResult.Ok) {
        return LocalDiscovery.NotFound("dead")
    }

    val health = io.probeHealth(result.baseUrl, timeoutMs)
    return LocalDiscovery.Found(result.baseUrl, result.pid, result.port, health)
}
