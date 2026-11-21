package ai.omnigent.android

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Base64
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

private const val UPGRADE_ERROR_BODY_LIMIT_BYTES = 8 * 1024

internal sealed class LegacyTicketFailure : Exception() {
    class BrowserUnavailable : LegacyTicketFailure()

    class Refused : LegacyTicketFailure()

    class NetworkDeadline(
        val lastError: IOException,
    ) : LegacyTicketFailure()

    class HttpStatus(
        val code: Int,
    ) : LegacyTicketFailure()

    class TimedOut : LegacyTicketFailure()
}

private sealed interface TicketPollResult {
    data class Token(
        val value: String,
    ) : TicketPollResult

    data class Failed(
        val reason: LegacyTicketFailure,
    ) : TicketPollResult
}

internal fun isUpgradeRequiredErrorResponse(
    responseCode: Int,
    errorStream: () -> InputStream?,
): Boolean {
    if (responseCode != 400) return false
    val errorBody =
        errorStream()?.use { stream ->
            val bytes = ByteArray(UPGRADE_ERROR_BODY_LIMIT_BYTES)
            var count = 0
            while (count < bytes.size) {
                val read = stream.read(bytes, count, bytes.size - count)
                if (read <= 0) break
                count += read
            }
            String(bytes, 0, count, Charsets.UTF_8)
        } ?: return false
    return isUpgradeRequiredResponse(responseCode, errorBody)
}

/**
 * Legacy ticket sign-in for OIDC servers whose manifest doesn't list this app's native
 * redirect. Deprecated: removal targeted for Android 0.3.0, once servers ship native sign-in.
 *
 * Drives the RFC 8252 login flow for the shell: authenticate in the system
 * browser — a real browser, so Google sign-in (which blocks embedded WebViews
 * with `disallowed_useragent`) and passkeys (which need the browser / a password
 * manager) both work — then bridge the resulting session into the WebView, whose
 * cookie store is isolated from the browser's.
 *
 * Reuses the server's existing browser-login endpoints (the same ones the
 * `omnigent login` CLI uses, no server change):
 *   1. `POST /auth/cli-login` with a PKCE S256 challenge -> `{ticket, login_url}`
 *   2. open `login_url` in the browser; the user authenticates and approves
 *      the consent page; that fulfills the ticket server-side
 *   3. `GET /auth/cli-poll?ticket=...` with `X-Omnigent-Code-Verifier` -> `{token}`
 *      once fulfilled (the verifier proves this client created the ticket)
 *
 * That `token` is exactly the session-cookie JWT (the server validates the same
 * HS256 JWT as either the session cookie or a `Bearer`), so [MainActivity]
 * injects it into the WebView's CookieManager and reloads — authenticated.
 */
class OidcLoginManager {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val inFlight = AtomicBoolean(false)

    // Held only for the duration of a login; nulled by [cancel]/[shutdown] so a
    // poll that finishes after a server switch or host destroy can neither inject
    // a stale token nor invoke into a dead Activity.
    @Volatile private var sessionCallback: ((String) -> Unit)? = null

    @Volatile private var currentTask: Future<*>? = null

    // Stamped into each flow at start() and bumped by cancel(). The queued
    // browser launch re-checks it at execution time, so a launch posted before a
    // cancel/server switch/destroy can never open the obsolete server's URL.
    private val flowGeneration = AtomicInteger(0)

    internal var pollIntervalMillis = POLL_INTERVAL_MS
    internal var pollTimeoutMillis = POLL_TIMEOUT_MS
    internal var browserLauncher: (Activity, String) -> Boolean = ::launchTab
    internal var pollConnectionFactory: (URL) -> HttpURLConnection = {
        it.openConnection() as HttpURLConnection
    }

    /**
     * Begin a login against [origin] (the pinned server). Opens the browser and
     * polls in the background; [onSession] is invoked on the main thread with the
     * session JWT once the browser flow completes.
     *
     * Returns true if this call started a flow, or false if one was already in
     * flight (a second concurrent call is ignored). The caller uses the result so
     * a no-op call isn't counted against a retry budget.
     */
    fun start(
        activity: Activity,
        origin: String,
        onSession: (String) -> Unit,
        onUpdateRequired: () -> Unit = {},
        onBrowserOpened: () -> Unit = {},
        onTicketFailure: (Throwable) -> Unit = {},
    ): Boolean {
        if (!inFlight.compareAndSet(false, true)) return false
        sessionCallback = onSession
        val generation = flowGeneration.incrementAndGet()

        fun postCurrent(action: () -> Unit) {
            main.post {
                if (generation == flowGeneration.get() &&
                    !activity.isFinishing && !activity.isDestroyed
                ) {
                    action()
                }
            }
        }
        currentTask =
            io.submit {
                var token: String? = null
                try {
                    val codeVerifier = generateCodeVerifier()
                    val ticket = requestTicket(origin, deriveCodeChallenge(codeVerifier))
                    authLog("cli-login -> ${if (ticket != null) "ticket ok" else "FAILED"}")
                    if (ticket == null) {
                        postCurrent {
                            onTicketFailure(LegacyTicketFailure.Refused())
                        }
                    } else {
                        postCurrent {
                            if (browserLauncher(activity, origin + ticket.loginUrl)) {
                                onBrowserOpened()
                            } else {
                                onTicketFailure(LegacyTicketFailure.BrowserUnavailable())
                            }
                        }
                        val pollResult = pollForToken(origin, ticket.id, codeVerifier)
                        token = (pollResult as? TicketPollResult.Token)?.value
                        authLog(
                            "poll -> ${if (token != null) "token (len=${token.length})" else "no token"}",
                        )
                        if (pollResult is TicketPollResult.Failed &&
                            !Thread.currentThread().isInterrupted
                        ) {
                            postCurrent {
                                onTicketFailure(pollResult.reason)
                            }
                        }
                    }
                } catch (_: InterruptedException) {
                    // shutdown() interrupted the poll — the host is going away; drop.
                } catch (_: UpgradeRequiredException) {
                    postCurrent(onUpdateRequired)
                } catch (t: Throwable) {
                    authLog("login flow error: ${t.javaClass.simpleName}")
                    postCurrent { onTicketFailure(t) }
                } finally {
                    inFlight.set(false)
                }
                val result = token
                // sessionCallback is null once shutdown() ran — never invoke into a
                // destroyed host.
                if (result != null) main.post { sessionCallback?.invoke(result) }
            }
        return true
    }

    /**
     * Cancel an in-flight login without tearing down the executor. Safe to call
     * when switching servers: nulls the callback (so a late-arriving token is
     * never injected), resets [inFlight] (so a new login can start immediately),
     * and interrupts the polling thread (stops wasted network I/O). Both this and
     * the token-delivery lambda run on the main thread, so the null is always
     * visible before the lambda can fire.
     */
    fun cancel() {
        flowGeneration.incrementAndGet() // invalidates any queued browser launch
        sessionCallback = null
        inFlight.set(false)
        currentTask?.cancel(true) // interrupts the polling sleep
        currentTask = null
    }

    /** Cancel any in-flight login and release the executor. Call from onDestroy. */
    fun shutdown() {
        cancel()
        io.shutdownNow()
    }

    private data class Ticket(
        val id: String,
        val loginUrl: String,
    )

    private class UpgradeRequiredException : Exception()

    private fun generateCodeVerifier(): String {
        val bytes = ByteArray(32)
        SecureRandom().nextBytes(bytes)
        return Base64.encodeToString(bytes, BASE64_URL_FLAGS)
    }

    private fun deriveCodeChallenge(codeVerifier: String): String {
        val digest =
            MessageDigest
                .getInstance("SHA-256")
                .digest(codeVerifier.toByteArray(Charsets.US_ASCII))
        return Base64.encodeToString(digest, BASE64_URL_FLAGS)
    }

    private fun requestTicket(
        origin: String,
        codeChallenge: String,
    ): Ticket? {
        val conn = (URL("$origin/auth/cli-login").openConnection() as HttpURLConnection)
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        conn.connectTimeout = HTTP_TIMEOUT_MS
        conn.readTimeout = HTTP_TIMEOUT_MS
        return try {
            val body =
                JSONObject()
                    .put("code_challenge", codeChallenge)
                    .put("code_challenge_method", "S256")
                    .toString()
            conn.outputStream.use { it.write(body.toByteArray()) }
            val responseCode = conn.responseCode
            if (responseCode != 200) {
                if (isUpgradeRequiredErrorResponse(responseCode) { conn.errorStream }) {
                    throw UpgradeRequiredException()
                }
                throw LegacyTicketFailure.HttpStatus(responseCode)
            }
            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            val id = json.optString("ticket").ifEmpty { return null }
            if (isUpgradeRequiredTicket(id)) throw UpgradeRequiredException()
            val loginUrl = json.optString("login_url").ifEmpty { return null }
            // The browser hand-off must stay on the pinned origin: [start]
            // concatenates this onto it, so only a relative path may pass — an
            // absolute URL or a scheme-relative `//host` would send the one-time
            // ticket flow to a server-chosen destination instead.
            if (!loginUrl.startsWith("/") || loginUrl.startsWith("//")) return null
            Ticket(id, loginUrl)
        } finally {
            conn.disconnect()
        }
    }

    private fun launchTab(
        activity: Activity,
        url: String,
    ): Boolean {
        // Full system browser (not a Custom Tab): the IdP flow page renders blank
        // in an in-app Custom Tab on some setups but works in the browser. Still
        // RFC 8252 — the system browser is the canonical external user-agent.
        authLog("opening login in browser") // URL carries the one-time ticket — not logged
        val intent =
            Intent(
                Intent.ACTION_VIEW,
                Uri.parse(url),
            ).addCategory(Intent.CATEGORY_BROWSABLE)
        return runCatching { activity.startActivity(intent) }.isSuccess
    }

    private fun pollForToken(
        origin: String,
        ticket: String,
        codeVerifier: String,
    ): TicketPollResult {
        val deadline = System.currentTimeMillis() + pollTimeoutMillis
        val encoded = Uri.encode(ticket)
        var lastNetworkError: IOException? = null
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(pollIntervalMillis) // throws InterruptedException on shutdownNow()
            val conn =
                pollConnectionFactory(
                    URL("$origin/auth/cli-poll?ticket=$encoded"),
                )
            conn.requestMethod = "GET"
            conn.setRequestProperty("X-Omnigent-Code-Verifier", codeVerifier)
            conn.connectTimeout = HTTP_TIMEOUT_MS
            conn.readTimeout = HTTP_TIMEOUT_MS
            try {
                val responseCode = conn.responseCode
                lastNetworkError = null
                when (responseCode) {
                    202 -> {
                        continue
                    }

                    // still pending
                    200 -> {
                        val body = conn.inputStream.bufferedReader().use { it.readText() }
                        val token = JSONObject(body).optString("token")
                        return if (token.isNotEmpty()) {
                            TicketPollResult.Token(token)
                        } else {
                            TicketPollResult.Failed(LegacyTicketFailure.Refused())
                        }
                    }

                    410 -> {
                        return TicketPollResult.Failed(LegacyTicketFailure.Refused())
                    }

                    else -> {
                        return TicketPollResult.Failed(
                            LegacyTicketFailure.HttpStatus(responseCode),
                        )
                    }
                }
            } catch (error: IOException) {
                if (Thread.currentThread().isInterrupted) throw InterruptedException()
                lastNetworkError = error
                continue // transient network error — keep polling until the deadline
            } finally {
                conn.disconnect()
            }
        }
        return TicketPollResult.Failed(
            lastNetworkError?.let(LegacyTicketFailure::NetworkDeadline)
                ?: LegacyTicketFailure.TimedOut(),
        )
    }

    private companion object {
        const val POLL_INTERVAL_MS = 2_000L
        const val POLL_TIMEOUT_MS = 5 * 60 * 1_000L // mirrors the CLI's 5-minute window
        const val HTTP_TIMEOUT_MS = 10_000 // connect + read timeout for the login endpoints
        const val BASE64_URL_FLAGS = Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING
    }
}
