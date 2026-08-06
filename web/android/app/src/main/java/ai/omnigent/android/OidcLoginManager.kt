package ai.omnigent.android

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.webkit.CookieManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.RejectedExecutionException

sealed interface LoginResult {
    data class Success(
        val token: String,
    ) : LoginResult

    data object Rejected : LoginResult

    data object TimedOut : LoginResult
}

/**
 * Drives the RFC 8252 login flow for the shell: authenticate in the system
 * browser — a real browser, so Google sign-in (which blocks embedded WebViews
 * with `disallowed_useragent`) and passkeys (which need the browser / a password
 * manager) both work — then bridge the resulting session into the WebView, whose
 * cookie store is isolated from the browser's.
 *
 * Reuses the server's existing browser-login endpoints (the same ones the
 * `omnigent login` CLI uses, no server change):
 *   1. `POST /auth/cli-login` -> `{ticket, login_url}`
 *   2. open `login_url` in the browser; the user authenticates; the OIDC
 *      callback fulfills the ticket server-side
 *   3. `GET /auth/cli-poll?ticket=...` -> `{token}` once fulfilled
 *
 * That `token` is exactly the session-cookie JWT (the server validates the same
 * HS256 JWT as either the session cookie or a `Bearer`), so [MainActivity]
 * injects it into the WebView's CookieManager and reloads — authenticated.
 */
class OidcLoginManager(
    private val pollIntervalMs: Long = DEFAULT_POLL_INTERVAL_MS,
    private val pollTimeoutMs: Long = DEFAULT_POLL_TIMEOUT_MS,
    private val clock: () -> Long = { SystemClock.elapsedRealtime() },
) {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val stateLock = Any()
    private var activeFlow: Flow? = null
    private var attachment: Attachment? = null
    private var heldResult: HeldResult? = null
    private var cancellationGeneration = 0L
    private var shutDown = false

    class Attachment internal constructor(
        internal val origin: String,
        internal var callback: ((String, LoginResult) -> Unit)?,
    )

    /** Attach the current main-thread host and replay a result completed while detached. */
    fun attach(
        origin: String,
        onResult: (String, LoginResult) -> Unit,
    ): Attachment {
        val newAttachment = Attachment(origin, onResult)
        var abandonedFuture: Future<*>? = null
        val held =
            synchronized(stateLock) {
                attachment?.callback = null
                attachment = newAttachment
                activeFlow?.takeIf { it.origin != origin }?.let { staleFlow ->
                    cancellationGeneration++
                    activeFlow = null
                    abandonedFuture = staleFlow.future
                }
                heldResult
                    ?.takeIf { it.flow.origin == origin }
                    .also { heldResult = null }
            }
        abandonedFuture?.cancel(true)
        held?.let { deliverOrHold(it.flow, it.result) }
        return newAttachment
    }

    /** Detach a host without interrupting its process-scoped login flow. */
    fun detach(attachment: Attachment) {
        synchronized(stateLock) {
            attachment.callback = null
            if (this.attachment === attachment) this.attachment = null
        }
    }

    /**
     * Begin a login against [origin] (the pinned server). Opens the browser and
     * polls in the background; the attached host receives the result on the main
     * thread when the flow succeeds, is rejected, or reaches its deadline.
     *
     * Returns true if this call started a flow, or false if one was already in
     * flight (a second concurrent call is ignored). The caller uses the result so
     * a no-op call isn't counted against a retry budget.
     */
    fun start(
        activity: Activity,
        origin: String,
    ): Boolean =
        synchronized(stateLock) {
            if (shutDown || activeFlow != null) return@synchronized false

            val flow = Flow(cancellationGeneration, origin)
            val context = activity.applicationContext
            activeFlow = flow
            try {
                flow.future =
                    io.submit {
                        val result = runFlow(context, origin, flow)
                        if (result == null) {
                            // Interrupted — the canceller already vacated the slot.
                            synchronized(stateLock) {
                                if (activeFlow === flow) activeFlow = null
                            }
                            return@submit
                        }
                        flow.completed = true
                        // The slot stays occupied until the result lands on the main
                        // thread, so a queued login trigger can't open a second
                        // concurrent browser flow ahead of this delivery.
                        main.post {
                            synchronized(stateLock) {
                                if (activeFlow === flow) activeFlow = null
                            }
                            // New starts keep the generation; only cancel/shutdown
                            // invalidate captured callbacks.
                            if (canDeliver(flow)) deliverOrHold(flow, result)
                        }
                    }
                true
            } catch (_: RejectedExecutionException) {
                if (activeFlow === flow) activeFlow = null
                false
            }
        }

    /** Run one flow off the main thread, or return null once it is interrupted. */
    private fun runFlow(
        context: Context,
        origin: String,
        flow: Flow,
    ): LoginResult? =
        try {
            val ticket = requestTicket(origin)
            authLog("cli-login -> ${if (ticket != null) "ticket ok" else "FAILED"}")
            if (ticket == null) {
                LoginResult.Rejected
            } else {
                main.post {
                    if (canDeliver(flow)) launchTab(context, origin + ticket.loginUrl)
                }
                pollForToken(origin, ticket.id).also(::logPollResult)
            }
        } catch (_: InterruptedException) {
            // Cancellation interrupted the poll; abandon the result.
            null
        } catch (t: Throwable) {
            authLog("login flow error: ${t.javaClass.simpleName}")
            LoginResult.Rejected
        }

    private fun logPollResult(result: LoginResult) {
        val outcome =
            when (result) {
                is LoginResult.Success -> "token (len=${result.token.length})"
                LoginResult.Rejected -> "rejected"
                LoginResult.TimedOut -> "timed out"
            }
        authLog("poll -> $outcome")
    }

    internal fun isInFlightForTest(): Boolean = synchronized(stateLock) { activeFlow != null }

    /** True while a finished flow still occupies the slot pending main-thread delivery. */
    internal fun isAwaitingDeliveryForTest(): Boolean =
        synchronized(stateLock) { activeFlow?.completed == true }

    internal fun hasAttachmentForTest(): Boolean =
        synchronized(stateLock) { attachment?.callback != null }

    internal fun hasHeldResultForTest(): Boolean = synchronized(stateLock) { heldResult != null }

    internal fun isShutDownForTest(): Boolean = synchronized(stateLock) { shutDown }

    /** Abandon the current flow while keeping the worker available for another login. */
    fun cancel() {
        val future =
            synchronized(stateLock) {
                cancellationGeneration++
                heldResult = null
                val flow = activeFlow
                activeFlow = null
                flow?.future
            }
        future?.cancel(true)
    }

    /**
     * Retire this manager permanently. One-way: nothing clears [shutDown], so on
     * the process instance this disables login for the rest of the process. An
     * Activity going away wants [cancel] + [detach], not this.
     */
    fun shutdown() {
        val future =
            synchronized(stateLock) {
                shutDown = true
                cancellationGeneration++
                attachment?.callback = null
                attachment = null
                heldResult = null
                val flow = activeFlow
                activeFlow = null
                flow?.future
            }
        future?.cancel(true)
        io.shutdownNow() // interrupts the polling sleep so the task exits promptly
    }

    private fun canDeliver(flow: Flow): Boolean = synchronized(stateLock) { canDeliverLocked(flow) }

    private fun deliverOrHold(
        flow: Flow,
        result: LoginResult,
    ) {
        val callback =
            synchronized(stateLock) {
                if (!canDeliverLocked(flow)) return
                val currentAttachment = attachment
                if (currentAttachment == null) {
                    if (result is LoginResult.Success) {
                        heldResult = HeldResult(flow, result)
                    } else {
                        authLog("dropping unattached login failure from ${flow.origin}")
                    }
                    return
                }
                if (currentAttachment.origin != flow.origin) {
                    authLog("dropping login result from stale origin ${flow.origin}")
                    return
                }
                currentAttachment.callback
            }
        callback?.invoke(flow.origin, result)
    }

    private fun canDeliverLocked(flow: Flow): Boolean =
        !shutDown && cancellationGeneration == flow.generation

    private inner class Flow(
        val generation: Long,
        val origin: String,
    ) {
        @Volatile
        var completed = false
        var future: Future<*>? = null
    }

    private data class HeldResult(
        val flow: Flow,
        val result: LoginResult,
    )

    private data class Ticket(
        val id: String,
        val loginUrl: String,
    )

    /**
     * Carry the WebView's cookies for [url] on a login-endpoint request.
     * HttpURLConnection has its own (empty) cookie store, so without this a
     * server behind a front-door auth proxy would 302 these endpoints to its
     * IdP even after the WebView already holds the proxy's session. Queried
     * per endpoint URL so Path-scoped cookies (e.g. Path=/auth) still match.
     */
    private fun attachWebViewCookies(
        conn: HttpURLConnection,
        url: String,
    ) {
        val cookies = runCatching { CookieManager.getInstance().getCookie(url) }.getOrNull()
        if (!cookies.isNullOrBlank()) conn.setRequestProperty("Cookie", cookies)
    }

    private fun requestTicket(origin: String): Ticket? {
        val url = "$origin/auth/cli-login"
        val conn = (URL(url).openConnection() as HttpURLConnection)
        conn.requestMethod = "POST"
        // A front-door auth proxy 302s this endpoint to its IdP's HTML login
        // page; following it would "succeed" with unparseable HTML. Fail fast.
        conn.instanceFollowRedirects = false
        attachWebViewCookies(conn, url)
        conn.connectTimeout = HTTP_TIMEOUT_MS
        conn.readTimeout = HTTP_TIMEOUT_MS
        return try {
            if (conn.responseCode != 200) return null
            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            val id = json.optString("ticket").ifEmpty { return null }
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
        context: Context,
        url: String,
    ) {
        // Full system browser (not a Custom Tab): the IdP flow page renders blank
        // in an in-app Custom Tab on some setups but works in the browser. Still
        // RFC 8252 — the system browser is the canonical external user-agent.
        authLog("opening login in browser") // URL carries the one-time ticket — not logged
        val intent =
            Intent(
                Intent.ACTION_VIEW,
                Uri.parse(url),
            ).addCategory(Intent.CATEGORY_BROWSABLE)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { context.startActivity(intent) }
    }

    private fun pollForToken(
        origin: String,
        ticket: String,
    ): LoginResult {
        val deadline = clock() + pollTimeoutMs
        val pollUrl = "$origin/auth/cli-poll?ticket=${Uri.encode(ticket)}"
        while (clock() < deadline) {
            Thread.sleep(pollIntervalMs) // throws InterruptedException on shutdownNow()
            if (clock() >= deadline) break

            var conn: HttpURLConnection? = null
            val body =
                try {
                    conn = URL(pollUrl).openConnection() as HttpURLConnection
                    conn.requestMethod = "GET"
                    // A proxied 302 is a terminal rejection, never redirect HTML.
                    conn.instanceFollowRedirects = false
                    attachWebViewCookies(conn, pollUrl)
                    conn.connectTimeout = HTTP_TIMEOUT_MS
                    conn.readTimeout = HTTP_TIMEOUT_MS
                    when (conn.responseCode) {
                        202 -> {
                            null
                        }

                        200 -> {
                            conn.inputStream.bufferedReader().use { it.readText() }
                        }

                        else -> {
                            return LoginResult.Rejected
                        }
                    }
                } catch (e: InterruptedException) {
                    throw e
                } catch (_: Throwable) {
                    if (Thread.currentThread().isInterrupted) throw InterruptedException()
                    continue // transient network error — keep polling until the deadline
                } finally {
                    conn?.disconnect()
                }
            if (body == null) continue

            // Parsing is outside the transient-error catch: a 200 with a bad
            // payload is terminal and must not be retried to the deadline.
            return try {
                JSONObject(body)
                    .optString("token")
                    .takeIf { it.isNotEmpty() }
                    ?.let { LoginResult.Success(it) }
                    ?: LoginResult.Rejected
            } catch (_: Throwable) {
                LoginResult.Rejected
            }
        }
        return LoginResult.TimedOut
    }

    companion object {
        private val processLock = Any()
        private var processManager: OidcLoginManager? = null

        internal val processInstance: OidcLoginManager
            get() =
                synchronized(processLock) {
                    processManager ?: OidcLoginManager().also { processManager = it }
                }

        internal fun resetProcessInstanceForTest() {
            val manager =
                synchronized(processLock) {
                    processManager.also { processManager = null }
                }
            manager?.shutdown()
        }

        private const val DEFAULT_POLL_INTERVAL_MS = 2_000L
        private const val DEFAULT_POLL_TIMEOUT_MS = 5 * 60 * 1_000L // mirrors the CLI's window
        private const val HTTP_TIMEOUT_MS = 10_000 // connect + read timeout for the login endpoints
    }
}
