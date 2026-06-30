package ai.omnigent.android

import android.app.Activity
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.browser.customtabs.CustomTabsIntent
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Drives the RFC 8252 login flow for the shell: authenticate in a Chrome Custom
 * Tab — a real browser, so Google sign-in (which blocks embedded WebViews with
 * `disallowed_useragent`) and passkeys (which need Chrome / Google Password
 * Manager) both work — then bridge the resulting session into the WebView, whose
 * cookie store is isolated from the Custom Tab's.
 *
 * Reuses the server's existing browser-login endpoints (the same ones the
 * `omnigent login` CLI uses, no server change):
 *   1. `POST /auth/cli-login` -> `{ticket, login_url}`
 *   2. open `login_url` in the Custom Tab; the user authenticates; the OIDC
 *      callback fulfills the ticket server-side
 *   3. `GET /auth/cli-poll?ticket=...` -> `{token}` once fulfilled
 *
 * That `token` is exactly the session-cookie JWT (the server validates the same
 * HS256 JWT as either the session cookie or a `Bearer`), so [MainActivity]
 * injects it into the WebView's CookieManager and reloads — authenticated.
 */
class OidcLoginManager {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val inFlight = AtomicBoolean(false)

    /**
     * Begin a login against [origin] (the pinned server). Opens a Custom Tab and
     * polls in the background; [onSession] is invoked on the main thread with the
     * session JWT once the browser flow completes. A second call while one is in
     * flight is ignored.
     */
    fun start(activity: Activity, origin: String, onSession: (String) -> Unit) {
        if (!inFlight.compareAndSet(false, true)) return
        io.execute {
            var token: String? = null
            try {
                val ticket = requestTicket(origin)
                if (ticket != null) {
                    main.post { launchTab(activity, origin + ticket.loginUrl) }
                    token = pollForToken(origin, ticket.id)
                }
            } catch (_: Throwable) {
                // Network/parse failure — login just doesn't complete; the user
                // can retry. Never surface raw errors (may carry URLs/tokens).
            } finally {
                inFlight.set(false)
            }
            val result = token
            if (result != null) main.post { onSession(result) }
        }
    }

    fun shutdown() {
        io.shutdown()
    }

    private data class Ticket(val id: String, val loginUrl: String)

    private fun requestTicket(origin: String): Ticket? {
        val conn = (URL("$origin/auth/cli-login").openConnection() as HttpURLConnection)
        conn.requestMethod = "POST"
        conn.connectTimeout = 10_000
        conn.readTimeout = 10_000
        return try {
            if (conn.responseCode != 200) return null
            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            val id = json.optString("ticket").ifEmpty { return null }
            val loginUrl = json.optString("login_url").ifEmpty { return null }
            Ticket(id, loginUrl)
        } finally {
            conn.disconnect()
        }
    }

    private fun launchTab(activity: Activity, url: String) {
        CustomTabsIntent.Builder()
            .setShowTitle(true)
            .build()
            .launchUrl(activity, Uri.parse(url))
    }

    private fun pollForToken(origin: String, ticket: String): String? {
        val deadline = System.currentTimeMillis() + POLL_TIMEOUT_MS
        val encoded = Uri.encode(ticket)
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(POLL_INTERVAL_MS)
            val conn = (URL("$origin/auth/cli-poll?ticket=$encoded").openConnection() as HttpURLConnection)
            conn.requestMethod = "GET"
            conn.connectTimeout = 10_000
            conn.readTimeout = 10_000
            try {
                when (conn.responseCode) {
                    202 -> continue // still pending
                    200 -> {
                        val body = conn.inputStream.bufferedReader().use { it.readText() }
                        return JSONObject(body).optString("token").ifEmpty { null }
                    }
                    else -> return null // 410 expired/rejected, or other
                }
            } catch (_: Throwable) {
                continue // transient network error — keep polling until the deadline
            } finally {
                conn.disconnect()
            }
        }
        return null
    }

    private companion object {
        const val POLL_INTERVAL_MS = 2_000L
        const val POLL_TIMEOUT_MS = 5 * 60 * 1_000L // mirrors the CLI's 5-minute window
    }
}
