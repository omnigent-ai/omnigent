package ai.omnigent.intellij.discovery

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

/** Tests for discoverLocalServer, exercised through the injectable DiscoveryIO boundary (no real fs/network). */
class DiscoveryTest {
    private fun io(
        readPidfile: () -> String? = { null },
        isPidAlive: (Int) -> Boolean = { true },
        probeHealth: (String, Long) -> HealthOutcome = { _, _ -> HealthOutcome.OK },
    ): DiscoveryIO = object : DiscoveryIO {
        override fun readPidfile(): String? = readPidfile()
        override fun isPidAlive(pid: Int): Boolean = isPidAlive(pid)
        override fun probeHealth(base: String, timeoutMs: Long): HealthOutcome = probeHealth(base, timeoutMs)
    }

    @Test
    fun `returns not-found when there is no pidfile`() {
        val r = discoverLocalServer(io(readPidfile = { null }))
        assertEquals(LocalDiscovery.NotFound("no-pidfile"), r)
    }

    @Test
    fun `returns malformed for a structurally invalid pidfile`() {
        val r = discoverLocalServer(io(readPidfile = { "garbage" }))
        assertEquals(LocalDiscovery.NotFound("malformed"), r)
    }

    @Test
    fun `returns dead when the pid is not alive`() {
        val r = discoverLocalServer(io(readPidfile = { "4242\n6767" }, isPidAlive = { false }))
        assertEquals(LocalDiscovery.NotFound("dead"), r)
    }

    @Test
    fun `returns a found target with its health outcome when alive`() {
        val r = discoverLocalServer(
            io(
                readPidfile = { "4242\n6767" },
                isPidAlive = { true },
                probeHealth = { _, _ -> HealthOutcome.OK },
            ),
        )
        assertEquals(LocalDiscovery.Found("http://127.0.0.1:6767", 4242, 6767, HealthOutcome.OK), r)
    }
}
