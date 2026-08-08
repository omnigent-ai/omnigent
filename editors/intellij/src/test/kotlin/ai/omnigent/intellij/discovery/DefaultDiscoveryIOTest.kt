package ai.omnigent.intellij.discovery

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Exercises the real `defaultDiscoveryIO` (fs/OS/network-backed). The fake-IO
 * discovery tests never touch it, so a self-recursive delegation here would go
 * unnoticed until runtime — these assertions fail fast (StackOverflowError) if
 * the IO methods bind to themselves instead of the top-level implementations.
 */
class DefaultDiscoveryIOTest {
    @Test
    fun `isPidAlive delegates to the OS probe, not itself`() {
        assertTrue(defaultDiscoveryIO.isPidAlive(ProcessHandle.current().pid().toInt()))
        assertFalse(defaultDiscoveryIO.isPidAlive(Int.MAX_VALUE))
    }

    @Test
    fun `probeHealth delegates to the HTTP probe, not itself`() {
        // Port 1 is not listening → a real connection error → UNREACHABLE.
        assertEquals(HealthOutcome.UNREACHABLE, defaultDiscoveryIO.probeHealth("http://127.0.0.1:1", 500))
    }
}
