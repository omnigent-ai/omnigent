package ai.omnigent.android

import android.content.Context

/**
 * Persistence for the connected server URL plus a recent-servers list, backing
 * [ConnectActivity]. Mirrors the iOS shell's `ConnectView` model (entry +
 * recents); the native UI here is intentionally minimal.
 *
 * Organization presets (see [ManagedConfig]) are folded into the offered server
 * list only. They are never auto-connected and never written to prefs, so the
 * user always chooses the server and a later policy change is picked up on the
 * next read.
 */
class ServerStore(
    context: Context,
    /** Organization presets; injectable so tests need no restrictions shadow. */
    val managed: ManagedConfig = ManagedConfig.from(context),
) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /**
     * True once the user has picked a server. Presets deliberately don't count:
     * an admin preconfigures the list, but the user still taps to connect.
     */
    fun hasServer(): Boolean = !storedServerUrl().isNullOrBlank()

    /**
     * The current server, or the emulator-loopback debug default if unset. A bare
     * Databricks workspace root resolves to its `/omnigent` mount so the shell
     * lands on the app instead of the workspace landing page. Expanded on read,
     * not on write, so the stored/offered entry stays what the user typed.
     */
    fun currentServerUrl(): String {
        val stored = storedServerUrl() ?: return DEFAULT_DEBUG_SERVER
        return databricksWorkspaceUiUrl(stored) ?: stored
    }

    /**
     * The servers to offer in the UI: organization presets first, then recents
     * they don't already cover. Presets join the one list rather than getting a
     * section of their own.
     */
    fun offeredServers(): List<String> =
        managed.serverUrls + recentServers().filterNot(managed::includes)

    /** Recently-connected servers, most recent first. */
    fun recentServers(): List<String> =
        prefs
            .getString(KEY_RECENTS, null)
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

    private fun storedServerUrl(): String? = prefs.getString(KEY_CURRENT, null)

    private companion object {
        const val PREFS = "ai.omnigent.android.servers"
        const val KEY_CURRENT = "current_server_url"
        const val KEY_RECENTS = "recent_server_urls"
        const val MAX_RECENTS = 8

        // 10.0.2.2 is the host loopback from the Android emulator.
        const val DEFAULT_DEBUG_SERVER = "http://10.0.2.2:8000"
    }
}
