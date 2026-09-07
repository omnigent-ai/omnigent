package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.os.Bundle
import androidx.core.content.getSystemService

/**
 * Server URLs an organization preconfigured for this install, read from Android
 * managed configurations (the schema in `res/xml/app_restrictions.xml`). An
 * admin sets the `serverUrls` restriction from their EMM/MDM console to a
 * comma- or newline-separated list, most preferred first.
 *
 * Presets are offers only: [ServerStore] lists them ahead of recent servers, but
 * the user still picks one to connect, and may enter any other server. Nothing
 * here auto-connects or locks the app to a host.
 */
data class ManagedConfig(
    val serverUrls: List<String>,
) {
    /** True when [url] shares an origin with a preset, so it needn't be listed twice. */
    fun includes(url: String): Boolean {
        val origin = originOf(url) ?: return false
        return serverUrls.any { originOf(it) == origin }
    }

    companion object {
        /**
         * Restriction key. Deployed policies reference it by name, so it is a
         * public contract — never rename it.
         */
        const val KEY_SERVER_URLS = "serverUrls"

        val EMPTY = ManagedConfig(emptyList())

        fun from(context: Context): ManagedConfig =
            from(context.getSystemService<RestrictionsManager>()?.applicationRestrictions)

        /**
         * Parse a restrictions bundle. Unmanaged installs have no bundle at all,
         * and a managed one only carries keys the admin actually set — even keys
         * with an XML default value — so both a null bundle and a missing key are
         * normal. Unparseable entries are dropped rather than failing the read:
         * a typo in one URL shouldn't cost the user the whole list.
         */
        fun from(restrictions: Bundle?): ManagedConfig {
            val raw = restrictions?.getString(KEY_SERVER_URLS) ?: return EMPTY
            val urls =
                raw
                    .split('\n', '\r', ',')
                    .mapNotNull(::normalizeServerUrl)
                    // The app pins by origin, so same-origin entries are one server.
                    .distinctBy { originOf(it) ?: it }
                    .take(MAX_PRESETS)
            return ManagedConfig(urls)
        }

        /** Matches the recents cap; keeps a malformed policy from flooding the UI. */
        private const val MAX_PRESETS = 8
    }
}
