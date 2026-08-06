package ai.omnigent.android

import android.webkit.CookieManager
import android.webkit.RoboCookieManager
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements

/**
 * Serves cookies from [cookieForUrl], keyed by the exact URL queried, so tests
 * can exercise per-request lookups (e.g. Path-scoped cookies) that
 * Robolectric's host-keyed cookie store cannot express.
 */
@Implements(CookieManager::class)
class RecordingCookieManagerShadow {
    companion object {
        val queriedUrls = mutableListOf<String>()
        var cookieForUrl: (String) -> String? = { null }

        private val recordingInstance: CookieManager by lazy {
            object : RoboCookieManager() {
                override fun getCookie(url: String): String? {
                    synchronized(queriedUrls) { queriedUrls += url }
                    return cookieForUrl(url)
                }
            }
        }

        @JvmStatic
        @Implementation
        fun getInstance(): CookieManager = recordingInstance

        fun reset() {
            synchronized(queriedUrls) { queriedUrls.clear() }
            cookieForUrl = { null }
        }
    }
}
