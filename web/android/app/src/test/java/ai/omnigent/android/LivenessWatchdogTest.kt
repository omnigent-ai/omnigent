package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class LivenessWatchdogTest {
    @Test
    fun `cached old web readiness and heartbeat loss time out deterministically`() {
        val scheduler = FakeScheduler()
        var failures = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = { failures++ })

        watchdog.beginInitialWindow()
        scheduler.advanceBy(19_999)
        assertEquals(0, failures)
        scheduler.advanceBy(1)
        assertEquals(1, failures)

        watchdog.beginInitialWindow()
        watchdog.protocolReady(1, 1)
        scheduler.advanceBy(14_999)
        watchdog.heartbeat()
        scheduler.advanceBy(14_999)
        assertEquals(1, failures)
        scheduler.advanceBy(1)
        assertEquals(2, failures)
    }

    @Test
    fun `protocol mismatch fails immediately`() {
        val scheduler = FakeScheduler()
        var incompatibilities = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = {}) { incompatibilities++ }

        watchdog.beginInitialWindow()
        assertFalse(watchdog.protocolReady(2, 1))
        assertEquals(1, incompatibilities)
        assertEquals(null, scheduler.remainingOrNull())
    }

    @Test
    fun `resume and auth return preserve compatibility through grace with heartbeats`() {
        val scheduler = FakeScheduler()
        var failures = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = { failures++ })

        watchdog.beginInitialWindow()
        watchdog.protocolReady(1, 1)
        watchdog.setActive(false)
        scheduler.advanceBy(60_000)
        assertEquals(0, failures)
        watchdog.setActive(true)
        assertEquals(LivenessWatchdog.INITIAL_READY_TIMEOUT_MS, scheduler.remaining())
        scheduler.advanceBy(14_000)
        watchdog.heartbeat()
        scheduler.advanceBy(14_000)
        assertEquals(0, failures)

        watchdog.setOnPinnedOrigin(false)
        scheduler.advanceBy(60_000)
        assertEquals(0, failures)
        watchdog.setOnPinnedOrigin(true)
        assertEquals(LivenessWatchdog.INITIAL_READY_TIMEOUT_MS, scheduler.remaining())
        scheduler.advanceBy(14_000)
        watchdog.heartbeat()
        scheduler.advanceBy(14_000)
        assertEquals(0, failures)
    }

    @Test
    fun `new document resets compatibility and requires readiness`() {
        val scheduler = FakeScheduler()
        var failures = 0
        val watchdog = LivenessWatchdog(scheduler, onTimeout = { failures++ })

        watchdog.beginInitialWindow()
        watchdog.protocolReady(1, 1)
        watchdog.beginInitialWindow()
        scheduler.advanceBy(10_000)
        watchdog.heartbeat()
        scheduler.advanceBy(10_000)
        assertEquals(1, failures)
    }

    private class FakeScheduler : WatchdogScheduler {
        private var now = 0L
        private var due: Long? = null
        private var action: (() -> Unit)? = null

        override fun schedule(
            delayMs: Long,
            action: () -> Unit,
        ) {
            due = now + delayMs
            this.action = action
        }

        override fun cancel() {
            due = null
            action = null
        }

        fun advanceBy(ms: Long) {
            now += ms
            if (due?.let { now >= it } == true) {
                val pending = action
                cancel()
                pending?.invoke()
            }
        }

        fun remaining(): Long = requireNotNull(due) - now

        fun remainingOrNull(): Long? = due?.minus(now)
    }
}
