package ai.omnigent.intellij.discovery

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class HealthTest {
    @Test
    fun `200 plus status ok body maps to ok`() {
        assertEquals(
            HealthOutcome.OK,
            interpretHealth(HealthObservation(status = 200, body = mapOf("status" to "ok"))),
        )
    }

    @Test
    fun `200 without an ok status body maps to unhealthy`() {
        assertEquals(
            HealthOutcome.UNHEALTHY,
            interpretHealth(HealthObservation(status = 200, body = mapOf("status" to "degraded"))),
        )
        assertEquals(HealthOutcome.UNHEALTHY, interpretHealth(HealthObservation(status = 200, body = null)))
    }

    @Test
    fun `non-200 maps to unhealthy`() {
        assertEquals(
            HealthOutcome.UNHEALTHY,
            interpretHealth(HealthObservation(status = 503, body = mapOf("status" to "ok"))),
        )
    }

    @Test
    fun `timeout maps to timeout`() {
        assertEquals(HealthOutcome.TIMEOUT, interpretHealth(HealthObservation(timedOut = true)))
    }

    @Test
    fun `network error maps to unreachable`() {
        assertEquals(HealthOutcome.UNREACHABLE, interpretHealth(HealthObservation(networkError = true)))
    }
}
