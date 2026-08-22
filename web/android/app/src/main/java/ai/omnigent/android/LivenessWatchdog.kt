package ai.omnigent.android

internal interface WatchdogScheduler {
    fun schedule(
        delayMs: Long,
        action: () -> Unit,
    )

    fun cancel()
}

internal class LivenessWatchdog(
    private val scheduler: WatchdogScheduler,
    private val onTimeout: () -> Unit,
    private val onIncompatible: () -> Unit = onTimeout,
) {
    private var active = true
    private var onPinnedOrigin = true
    private var compatible = false

    fun beginInitialWindow() {
        compatible = false
        arm(INITIAL_READY_TIMEOUT_MS)
    }

    fun protocolReady(
        version: Int,
        expectedVersion: Int,
    ): Boolean {
        if (version != expectedVersion) {
            scheduler.cancel()
            onIncompatible()
            return false
        }
        compatible = true
        arm(HEARTBEAT_TIMEOUT_MS)
        return true
    }

    fun heartbeat() {
        if (compatible) arm(HEARTBEAT_TIMEOUT_MS)
    }

    fun setActive(value: Boolean) {
        if (active == value) return
        active = value
        if (value) arm(INITIAL_READY_TIMEOUT_MS) else scheduler.cancel()
    }

    fun setOnPinnedOrigin(value: Boolean) {
        if (onPinnedOrigin == value) return
        onPinnedOrigin = value
        if (value) arm(INITIAL_READY_TIMEOUT_MS) else scheduler.cancel()
    }

    fun cancel() = scheduler.cancel()

    private fun arm(delayMs: Long) {
        scheduler.cancel()
        if (active && onPinnedOrigin) scheduler.schedule(delayMs, onTimeout)
    }

    companion object {
        const val INITIAL_READY_TIMEOUT_MS = 20_000L
        const val HEARTBEAT_TIMEOUT_MS = 15_000L
    }
}
