package ai.omnigent.android

import android.content.Context

/**
 * Persistence for the connected server URL plus a recent-servers list, backing
 * [ConnectActivity]. Mirrors the iOS shell's `ConnectView` model (entry +
 * recents); the native UI here is intentionally minimal.
 */
class ServerStore(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun hasServer(): Boolean = !prefs.getString(KEY_CURRENT, null).isNullOrBlank()

    /** The current server, or the emulator-loopback debug default if unset. */
    fun currentServerUrl(): String = prefs.getString(KEY_CURRENT, null) ?: DEFAULT_DEBUG_SERVER

    /** Recently-connected servers, most recent first. */
    fun recentServers(): List<String> =
        prefs.getString(KEY_RECENTS, null)
            ?.split("\n")
            ?.filter { it.isNotBlank() }
            .orEmpty()

    /** Set the current server and push it to the front of the recents list. */
    fun connect(url: String) {
        val recents = (listOf(url) + recentServers()).distinct().take(MAX_RECENTS)
        prefs
            .edit()
            .putString(KEY_CURRENT, url)
            .putString(KEY_RECENTS, recents.joinToString("\n"))
            .apply()
    }

    private companion object {
        const val PREFS = "ai.omnigent.android.servers"
        const val KEY_CURRENT = "current_server_url"
        const val KEY_RECENTS = "recent_server_urls"
        const val MAX_RECENTS = 8
        // 10.0.2.2 is the host loopback from the Android emulator.
        const val DEFAULT_DEBUG_SERVER = "http://10.0.2.2:8000"
    }
}
