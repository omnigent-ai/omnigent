package ai.omnigent.intellij.discovery

import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class LivenessTest {
    @Test
    fun `reports the current process as alive`() {
        assertTrue(isPidAlive(ProcessHandle.current().pid().toInt()))
    }

    @Test
    fun `reports an almost-certainly-unused pid as not alive`() {
        assertFalse(isPidAlive(Int.MAX_VALUE))
    }

    @Test
    fun `reports a non-positive pid as not alive`() {
        assertFalse(isPidAlive(0))
        assertFalse(isPidAlive(-1))
    }
}
