package ai.omnigent.android

import java.util.UUID

/**
 * One-time tokens marking a connect the user just asked for. Android restores a killed
 * activity with its original intent, so only an unused token from this process counts.
 */
internal object UserConnectRequests {
    private var pending: String? = null

    /** A token for the intent of a connect the user started. */
    @Synchronized
    fun issue(): String = UUID.randomUUID().toString().also { pending = it }

    /** True once, for the token this process issued last. */
    @Synchronized
    fun consume(token: String?): Boolean {
        if (token == null || token != pending) return false
        pending = null
        return true
    }
}
